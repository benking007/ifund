#!/usr/bin/env python3
"""fund_holdings worker：拉取股票/债券持仓，按基金全量替换。"""
from __future__ import annotations

# 与净值 worker 共享同一套请求重试骨架，差异仅在数据源和落库模型。
# pylint: disable=duplicate-code

import os
import sys
from pathlib import Path

_BACKEND_DIR = os.getenv("IFUND_BACKEND_DIR") or str(Path(__file__).resolve().parents[3])
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)
os.chdir(_BACKEND_DIR)

# pylint: disable=wrong-import-position
import datetime
import logging
import random
import re
import threading
import time

import akshare as ak  # pylint: disable=import-error
import akshare.fund.fund_portfolio_em as _fund_portfolio_em  # pylint: disable=import-error
import requests

from app.common import worker_base
from app.common.network import (
    HTTP_TIMEOUT,
    MAX_NETWORK_ATTEMPTS,
    NetworkRetryExhausted,
    is_retryable_network_error,
)
from app.fund_holdings.crud import holdings_crud

_QUARTER_RE = re.compile(r"(\d{4}).*?([1-4])\s*季度")
_REQUEST_TIMEOUT = HTTP_TIMEOUT
_BACKOFF_BASE_SECONDS = 2
_RETRY_JITTER = 0.3
logger = logging.getLogger(__name__)


class _RequestsProxy:
    """只为 AkShare 持仓模块补默认 timeout，不改动全局 requests 模块。"""

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
        """注入默认超时后调用线程本地 Session。"""
        kwargs.setdefault("timeout", _REQUEST_TIMEOUT)
        return self._session().get(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._requests_module, name)


# AkShare 1.18.81 的持仓接口没有 timeout 参数，且内部直接调用 requests.get。
# 替换该模块自己的 requests 引用，避免并发时修改全局 requests.get。
if not isinstance(_fund_portfolio_em.requests, _RequestsProxy):
    _fund_portfolio_em.requests = _RequestsProxy(requests)


def _normalize_quarter(text: str) -> str:
    """「2024年1季度」→「2024Q1」。"""
    match = _QUARTER_RE.search(text or "")
    return f"{match.group(1)}Q{match.group(2)}" if match else (text or "").strip()


def _call_with_retry(label, func):
    """AkShare 仅对网络错误重试，总尝试次数不超过三次。"""
    for attempt in range(MAX_NETWORK_ATTEMPTS):
        try:
            return func()
        except KeyError:
            # AkShare 对无债券持仓的空表会在内部访问缺失列时抛出 KeyError；
            # 这属于合法的「无数据」，由债券行转换层统一处理，其他 KeyError 继续上抛。
            raise
        except Exception as exc:  # pylint: disable=broad-exception-caught
            if not is_retryable_network_error(exc):
                logger.warning("%s 业务性失败，不重试：%s", label, exc)
                raise
            if attempt + 1 >= MAX_NETWORK_ATTEMPTS:
                logger.warning("%s 网络失败（已尝试 %d 次）：%s", label, attempt + 1, exc)
                raise NetworkRetryExhausted(label, attempt + 1) from exc
            delay = (_BACKOFF_BASE_SECONDS ** (attempt + 1)) * random.uniform(
                1 - _RETRY_JITTER, 1 + _RETRY_JITTER
            )
            logger.warning(
                "%s 第 %d 次失败，将在 %.2f 秒后重试：%s",
                label, attempt + 1, delay, exc,
            )
            time.sleep(delay)
    raise RuntimeError("重试循环意外结束")


def _stock_rows(code, year, now):
    frame = _call_with_retry(
        f"基金 {code} 股票持仓 {year}",
        lambda: ak.fund_portfolio_hold_em(symbol=code, date=str(year)),
    )
    rows = []
    for _, row in frame.iterrows():
        rows.append({
            "fund_code": code,
            "quarter": _normalize_quarter(str(row.get("季度", ""))),
            "holding_type": "stock",
            "asset_code": str(row.get("股票代码", "")).strip(),
            "asset_name": str(row.get("股票名称", "")).strip(),
            "hold_ratio": worker_base.safe_float(row.get("占净值比例")),
            "hold_amount": worker_base.safe_float(row.get("持股数")),
            "hold_market_value": worker_base.safe_float(row.get("持仓市值")),
            "raw_data": "{}",
            "fetch_time": now,
        })
    return rows


def _bond_rows(code, year, now):
    """拉取债券持仓。若 AkShare 内部因空表触发 KeyError，视为无债券持仓。"""
    label = f"基金 {code} 债券持仓 {year}"
    try:
        frame = _call_with_retry(
            label,
            lambda: ak.fund_portfolio_bond_hold_em(symbol=code, date=str(year)),
        )
    except KeyError as exc:
        # AkShare 债券接口对无数据的基金内部访问 DataFrame 缺失列时抛出 KeyError，
        # 不按异常文本判断具体列名（列名可能随 AkShare 版本变化）。
        logger.warning("%s 返回空债券表，按无债券持仓处理：%s", label, exc)
        return []
    rows = []
    for _, row in frame.iterrows():
        rows.append({
            "fund_code": code,
            "quarter": _normalize_quarter(str(row.get("季度", ""))),
            "holding_type": "bond",
            "asset_code": str(row.get("债券代码", "")).strip(),
            "asset_name": str(row.get("债券名称", "")).strip(),
            "hold_ratio": worker_base.safe_float(row.get("占净值比例")),
            "hold_amount": None,  # 债券行强制置 None
            "hold_market_value": worker_base.safe_float(row.get("持仓市值")),
            "raw_data": "{}",
            "fetch_time": now,
        })
    return rows


def _dedup(rows):
    seen = {}
    for row in rows:
        key = (row["fund_code"], row["quarter"], row["holding_type"], row["asset_code"])
        seen[key] = row
    return list(seen.values())


def _previous_quarter(today: datetime.date) -> str:
    """返回上一个自然季度，形如 ``2026Q1``。"""
    current_quarter = (today.month - 1) // 3 + 1
    if current_quarter == 1:
        return f"{today.year - 1}Q4"
    return f"{today.year}Q{current_quarter - 1}"


def _process_one(code):
    today = datetime.date.today()
    stored = holdings_crud.stored_latest(code)
    if stored and stored >= _previous_quarter(today):
        return "skip"
    now = datetime.datetime.now().isoformat()
    year = today.year
    rows = []
    for target_year in (year - 1, year):
        rows += _stock_rows(code, target_year, now)
        rows += _bond_rows(code, target_year, now)
    holdings_crud.upsert(code, _dedup(rows))
    return "success"


if __name__ == "__main__":
    worker_base.main(_process_one)
