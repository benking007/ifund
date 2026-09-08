#!/usr/bin/env python3
"""fund_nav worker：增量拉取单位净值走势 + 累计收益率走势。"""

from __future__ import annotations

# 与持仓 worker 共享同一套请求重试骨架，差异仅在数据源和落库模型。
# pylint: disable=duplicate-code
import os
import sys
from pathlib import Path

_BACKEND_DIR = os.getenv("IFUND_BACKEND_DIR") or str(
    Path(__file__).resolve().parents[3]
)
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)
os.chdir(_BACKEND_DIR)

# pylint: disable=wrong-import-position
import datetime
import logging
import random
import threading
import time

import akshare as ak  # pylint: disable=import-error
import akshare.fund.fund_em as _fund_em  # pylint: disable=import-error
import requests

from app import db as database
from app.common import worker_base
from app.common.network import (
    HTTP_TIMEOUT,
    MAX_NETWORK_ATTEMPTS,
    NetworkRetryExhausted,
    is_retryable_network_error,
)
from app.fund_nav import sync_state
from app.fund_nav.crud import nav_crud, repair_crud
from app.fund_nav.fetch import eastmoney, no_nav_blacklist
from app.fund_nav.fetch.errors import (
    AKSHARE_EMPTY,
    NoNavDataError,
    is_no_nav_data_error,
)
from app.trade_calendar.crud import calendar_crud

_REQUEST_TIMEOUT = HTTP_TIMEOUT
_BACKOFF_BASE_SECONDS = 2
_RETRY_JITTER = 0.3
logger = logging.getLogger(__name__)


def _date_text(value):
    """把数据库 DATE 或字符串统一为可比较的 ISO 日期。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime.datetime):
        return value.date().isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    return str(value)


def _akshare_enabled() -> bool:
    """AkShare 只允许显式开关启用。"""
    return os.getenv("IFUND_ENABLE_AKSHARE_FALLBACK", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _write_nav_with_watermark(code, nav_rows, cum_rows=None):
    """把事实写入和净值水位推进放在同一事务。"""
    if not nav_rows:
        return
    with database.get_db().transaction():
        nav_crud.insert_rows("fund_nav", nav_rows)
        if cum_rows:
            nav_crud.insert_rows("fund_cum_return", cum_rows)
        sync_state.set_watermark(
            code,
            "nav",
            max(row["trade_date"] for row in nav_rows),
        )


class _RequestsProxy:
    """只为 AkShare 基金净值模块补默认 timeout，不改动全局 requests 模块。"""

    _ifund_timeout_proxy = True

    def __init__(self, requests_module):
        self._requests_module = requests_module
        self._local = threading.local()

    def _session(self):
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._requests_module.Session()
            self._local.session = session
        return session

    def get(self, *args, **kwargs):
        """注入默认 timeout 后调用线程本地 Session。"""
        kwargs.setdefault("timeout", _REQUEST_TIMEOUT)
        return self._session().get(*args, **kwargs)

    def __getattr__(self, name):
        """把未覆盖的 requests 属性透传给原模块。"""
        return getattr(self._requests_module, name)


# AkShare 的基金净值接口没有 timeout 参数，且内部直接调用 requests.get。
# 替换该模块自己的 requests 引用，避免并发时修改全局 requests.get。
if not isinstance(_fund_em.requests, _RequestsProxy):
    _fund_em.requests = _RequestsProxy(requests)


def _call_with_retry(label, func, *args, **kwargs):
    """AkShare 仅对网络错误重试，总尝试次数不超过三次。"""
    for attempt in range(MAX_NETWORK_ATTEMPTS):
        try:
            return func(*args, **kwargs)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            if not is_retryable_network_error(exc):
                logger.warning("%s 业务性失败，不重试：%s", label, exc)
                raise
            if attempt + 1 >= MAX_NETWORK_ATTEMPTS:
                logger.warning(
                    "%s 网络失败（已尝试 %d 次）：%s", label, attempt + 1, exc
                )
                raise NetworkRetryExhausted(label, attempt + 1) from exc
            delay = (_BACKOFF_BASE_SECONDS ** (attempt + 1)) * random.uniform(
                1 - _RETRY_JITTER, 1 + _RETRY_JITTER
            )
            logger.warning(
                "%s 第 %d 次失败，将在 %.2f 秒后重试：%s",
                label,
                attempt + 1,
                delay,
                exc,
            )
            time.sleep(delay)
    raise RuntimeError(f"{label} 重试循环异常结束")  # pragma: no cover


def _acc_nav_map(code):
    """累计净值走势 → ``{trade_date: 累计净值}``。

    累计净值（复权口径，分红除息日不断崖）在「累计净值走势」接口里，
    「单位净值走势」接口**不返回**该列，故需单独拉一次按日期对齐。
    接口异常时返回空表，降级为只存单位净值。
    """
    try:
        frame = _call_with_retry(
            f"基金 {code} 累计净值走势",
            ak.fund_open_fund_info_em,
            symbol=code,
            indicator="累计净值走势",
        )
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return {}
    return {
        str(row["净值日期"]): worker_base.safe_float(row.get("累计净值"))
        for _, row in frame.iterrows()
    }


def _nav_rows(code, stored, now):
    frame = _call_with_retry(
        f"基金 {code} 单位净值走势",
        ak.fund_open_fund_info_em,
        symbol=code,
        indicator="单位净值走势",
    )
    if frame is None or frame.empty:
        raise NoNavDataError(f"基金 {code} 单位净值走势为空", reason=AKSHARE_EMPTY)
    acc_map = _acc_nav_map(code)
    rows = []
    for _, row in frame.iterrows():
        day = str(row["净值日期"])
        if stored and day <= stored:
            continue
        rows.append(
            {
                "fund_code": code,
                "trade_date": day,
                "nav": worker_base.safe_float(row.get("单位净值")),
                "acc_nav": acc_map.get(day),
                "daily_return": worker_base.safe_float(row.get("日增长率")),
                "fetch_time": now,
            }
        )
    return rows


def _cum_rows(code, stored, now):
    frame = _call_with_retry(
        f"基金 {code} 累计收益率走势",
        ak.fund_open_fund_info_em,
        symbol=code,
        indicator="累计收益率走势",
    )
    rows = []
    for _, row in frame.iterrows():
        day = str(row["日期"])
        if stored and day <= stored:
            continue
        cum_return = worker_base.safe_float(row.get("累计收益率"))
        if cum_return is None:
            continue
        rows.append(
            {
                "fund_code": code,
                "trade_date": day,
                "cum_return": cum_return,
                "fetch_time": now,
            }
        )
    return rows


def _process_one_akshare_full(code):
    """显式退避时用 AkShare 拉取全史；单源空值不写 blacklist。"""
    if no_nav_blacklist.is_blacklisted(code):
        return "skip"
    now = datetime.datetime.now().astimezone().isoformat()
    nav_stored = nav_crud.stored_latest(code, "fund_nav")
    try:
        nav_rows = _nav_rows(code, nav_stored, now)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        if is_no_nav_data_error(exc):
            logger.warning("akshare 单源无适用单位净值(%s)，不写黑名单：%s", code, exc)
            return "skip"
        attempts = exc.attempts if isinstance(exc, NetworkRetryExhausted) else 1
        logger.warning("akshare 全量净值失败(%s)，进入 repair：%s", code, exc)
        try:
            repair_crud.record_failure(code, "nav", str(exc), attempts=attempts)
        except Exception as queue_exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            logger.warning(
                "基金 %s 净值失败写入 repair 队列也失败：%s", code, queue_exc
            )
        return "fail"
    if not nav_rows:
        return "skip"
    cum_stored = nav_crud.stored_latest(code, "fund_cum_return")
    _write_nav_with_watermark(code, nav_rows, _cum_rows(code, cum_stored, now))
    return "success"


def _fetch_incremental(code, start, end):
    """逐基金入口仅使用东财；Tushare 只能由 daily_sync 按 nav_date 拉全市场。"""
    return eastmoney.fetch_nav_incremental(code, start, end)


def _process_one(code, watermark=None):
    if no_nav_blacklist.is_blacklisted(code):
        return "skip"
    base = _date_text(calendar_crud.base_trade_date())  # 当天未发布则取 T-1，不空拉
    nav_stored = _date_text(watermark)
    if not nav_stored:
        nav_stored = _date_text(nav_crud.stored_latest(code, "fund_nav"))
    if base and nav_stored and nav_stored >= base:
        return "skip"
    if not nav_stored:
        nav_stored = "2000-01-01"
    try:
        start = nav_stored
        end = base or datetime.datetime.now().astimezone().date().isoformat()
        rows = _fetch_incremental(code, start, end)
        now = datetime.datetime.now().astimezone().isoformat()
        nav_rows = []
        cum_rows = []
        for row in rows:
            # F10 返回含 start_date 边界，依赖 INSERT OR REPLACE 保持幂等。
            if row["nav"] is not None:
                nav_rows.append(
                    {
                        "fund_code": code,
                        "trade_date": row["trade_date"],
                        "nav": row["nav"],
                        "acc_nav": row["acc_nav"],
                        "daily_return": row["daily_return"],
                        "fetch_time": now,
                    }
                )
            if row["cum_return"] is not None:
                cum_rows.append(
                    {
                        "fund_code": code,
                        "trade_date": row["trade_date"],
                        "cum_return": row["cum_return"],
                        "fetch_time": now,
                    }
                )
        _write_nav_with_watermark(code, nav_rows, cum_rows)
        return "success"
    except NoNavDataError as exc:
        logger.warning("东财单源无适用净值(%s)，不写黑名单：%s", code, exc)
        return "skip"
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        if _akshare_enabled():
            logger.warning("东财增量失败(%s)，显式回退 akshare：%s", code, exc)
            return _process_one_akshare_full(code)
        logger.warning("东财增量失败(%s)，AkShare 默认关闭：%s", code, exc)
        return "fail"


if __name__ == "__main__":
    worker_base.main(_process_one)
