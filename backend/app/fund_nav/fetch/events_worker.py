#!/usr/bin/env python3
"""基金分红/拆分事件采集 worker。"""

from __future__ import annotations

# Batch and per-fund event mappers intentionally share field normalization.
# pylint: disable=duplicate-code

import datetime as dt
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_BACKEND_DIR = os.getenv("IFUND_BACKEND_DIR") or str(
    Path(__file__).resolve().parents[3]
)
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)
os.chdir(_BACKEND_DIR)

# pylint: disable=wrong-import-position
from app import db as database
from app.common import worker_base
from app.common.network import (
    MAX_NETWORK_ATTEMPTS,
    NetworkRetryExhausted,
    is_retryable_network_error,
)
from app.fund_nav.crud import div_split_crud, repair_crud
from app.fund_nav.fetch import tushare_client
from app.fund_nav.fetch.backfill_worker import (
    CHECKPOINT_EVERY,
    checkpoint_wal,
    ensure_batch_allowed,
    resolve_by_types,
)


logger = logging.getLogger(__name__)
TUSHARE_MAX_ATTEMPTS = MAX_NETWORK_ATTEMPTS
TUSHARE_INITIAL_BACKOFF_SECONDS = 1
TUSHARE_MAX_BACKOFF_SECONDS = 4
TUSHARE_REQUEST_INTERVAL_SECONDS = max(
    float(os.getenv("TUSHARE_INTERVAL_MS", "1000")) / 1000.0, 0.2
)
_EVENT_SCAN_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_scan_status (
    fund_code TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('pending', 'done', 'failed')),
    scanned_at TEXT NOT NULL
)
"""
_scan_status_lock = threading.Lock()
_scan_status_state = {"db_id": None}


class _IntervalRateLimiter:
    """进程内全局限速器，保证 Tushare 请求起始时间至少间隔 400ms。"""

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._lock = threading.Lock()
        self._next_at: float | None = None

    def wait(self) -> None:
        """等待并预留下一个请求时间槽。"""
        with self._lock:
            now = time.monotonic()
            wait_seconds = max(0.0, (self._next_at or now) - now)
            self._next_at = max(now, self._next_at or now) + self._interval
        if wait_seconds:
            time.sleep(wait_seconds)


_tushare_rate_limiter = _IntervalRateLimiter(TUSHARE_REQUEST_INTERVAL_SECONDS)


def call_tushare_with_retry(label, func, *args, **kwargs):
    """调用 Tushare；仅网络错误退避，总尝试次数不超过三次。"""
    for attempt in range(TUSHARE_MAX_ATTEMPTS):
        _tushare_rate_limiter.wait()
        try:
            return func(*args, **kwargs)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            if not is_retryable_network_error(exc):
                logger.warning("%s 业务性失败，不重试：%s", label, exc)
                raise
            if attempt + 1 >= TUSHARE_MAX_ATTEMPTS:
                logger.warning(
                    "%s 网络失败（已尝试 %d 次）：%s",
                    label,
                    attempt + 1,
                    exc,
                )
                raise NetworkRetryExhausted(label, attempt + 1) from exc
            delay = min(
                TUSHARE_MAX_BACKOFF_SECONDS,
                TUSHARE_INITIAL_BACKOFF_SECONDS * (2**attempt),
            )
            logger.warning(
                "%s 第 %d 次失败，将在 %.0f 秒后重试：%s",
                label,
                attempt + 1,
                delay,
                exc,
            )
            time.sleep(delay)
    raise RuntimeError("Tushare 重试循环意外结束")  # pragma: no cover


def _ensure_scan_status_table() -> None:
    """兼容未重启服务的旧库，首次使用时幂等创建状态表。"""
    db_id = id(database.get_db())
    if _scan_status_state["db_id"] == db_id:
        return
    with _scan_status_lock:
        if _scan_status_state["db_id"] != db_id:
            database.init_db(_EVENT_SCAN_SCHEMA)
            _scan_status_state["db_id"] = db_id


def get_event_scan_status(code: str) -> str | None:
    """读取基金最近一次事件扫描状态。"""
    _ensure_scan_status_table()
    row = database.select_one("event_scan_status", {"fund_code": f"eq.{code}"})
    return row.get("status") if row else None


def _set_event_scan_status(code: str, status: str) -> None:
    """记录事件扫描状态；同一基金只保留最新一行。"""
    _ensure_scan_status_table()
    database.batch_insert(
        "event_scan_status",
        [
            {
                "fund_code": code,
                "status": status,
                "scanned_at": dt.datetime.now().isoformat(timespec="seconds"),
            }
        ],
    )


def _record_event_failure(code: str, exc: Exception) -> None:
    """将事件扫描失败纳入现有 adj 修复队列。"""
    attempts = exc.attempts if isinstance(exc, NetworkRetryExhausted) else 1
    try:
        repair_crud.record_failure(
            code,
            "adj",
            f"event scan failed: {exc}",
            attempts=attempts,
        )
    except Exception as queue_exc:  # pylint: disable=broad-exception-caught
        logger.warning("基金 %s 事件失败入 repair 队列也失败：%s", code, queue_exc)


def _date(value: object) -> str | None:
    """把接口日期规整为 YYYY-MM-DD。"""
    if value is None:
        return None
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text or None


def _float(value: object) -> float | None:
    """把可选事件数值转成 float。"""
    try:
        return float(value) if value not in (None, "", "-") else None
    except (TypeError, ValueError):
        return None


# Tushare 无基金拆分接口（fund_split 不存在，返回「请指定正确的接口名」）。
# 首次确认接口不存在后全局禁用拆分通道，避免每只基金重复空跑。
_SPLIT_CHANNEL_OK = True


def _probe_split_channel() -> bool:
    """探测拆分接口可用性；接口不存在则全局禁用并返回 False。"""
    global _SPLIT_CHANNEL_OK  # pylint: disable=global-statement
    if not _SPLIT_CHANNEL_OK:
        return False
    try:
        call_tushare_with_retry(
            "基金拆分接口探测",
            tushare_client.fetch_fund_split,
            "000001",
            timeout=tushare_client.REQUEST_TIMEOUT,
        )
        return True
    except tushare_client.TushareError as exc:
        if "请指定正确的接口名" in str(exc):
            _SPLIT_CHANNEL_OK = False
            logger.warning("Tushare 无基金拆分接口（fund_split），拆分通道已全局禁用")
            return False
        raise


def fetch_event_rows(
    code: str, start_date: str | None = None, end_date: str | None = None
) -> list[dict]:
    """从 Tushare 两个事件源读取并转换为本地统一行。"""
    rows = []
    for raw in call_tushare_with_retry(
        f"基金 {code} 分红事件",
        tushare_client.fetch_fund_div,
        code,
        start_date,
        end_date,
        timeout=tushare_client.REQUEST_TIMEOUT,
    ):
        ex_date = _date(raw.get("ex_date") or raw.get("div_date"))
        if ex_date:
            rows.append(
                {
                    "fund_code": code,
                    "ts_code": tushare_client.resolve_ts_code(code),
                    "ex_date": ex_date,
                    "ann_date": _date(raw.get("ann_date")),
                    "record_date": _date(raw.get("record_date")),
                    "pay_date": _date(raw.get("pay_date")),
                    "event_type": "div",
                    "cash_per_unit": _float(
                        raw.get("div_cash") or raw.get("cash_per_unit")
                    ),
                    "split_ratio": None,
                    "source": "tushare",
                }
            )
    if _probe_split_channel():
        for raw in call_tushare_with_retry(
            f"基金 {code} 拆分事件",
            tushare_client.fetch_fund_split,
            code,
            start_date,
            end_date,
            timeout=tushare_client.REQUEST_TIMEOUT,
        ):
            ex_date = _date(raw.get("split_date") or raw.get("ex_date"))
            ratio = _float(raw.get("split_ratio"))
            if ex_date and ratio is not None and ratio > 0:
                rows.append(
                    {
                        "fund_code": code,
                        "ex_date": ex_date,
                        "event_type": "split",
                        "cash_per_unit": None,
                        "split_ratio": ratio,
                        "source": "tushare",
                    }
                )
    return rows


def sync_one(
    code: str, start_date: str | None = None, end_date: str | None = None
) -> dict:
    """采集并幂等写入单只基金事件。"""
    try:
        _set_event_scan_status(code, "pending")
        rows = fetch_event_rows(code, start_date, end_date)
        written, changed = div_split_crud.upsert_events_changed(rows)
        _set_event_scan_status(code, "done")
    except Exception as exc:  # pylint: disable=broad-exception-caught
        try:
            _set_event_scan_status(code, "failed")
        except Exception as status_exc:  # pylint: disable=broad-exception-caught
            logger.warning("基金 %s 事件状态写入失败：%s", code, status_exc)
        _record_event_failure(code, exc)
        raise
    return {"fund_code": code, "rows": written, "changed": changed}


def process_one(code: str) -> str:
    """worker_base 所需的单基金状态回调。"""
    try:
        sync_one(code)
        return "success"
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.exception("基金 %s 事件采集失败：%s", code, exc)
        return "fail"


def resolve_codes(
    codes: list[str] | None = None,
    fund_types: list[str] | None = None,
    *,
    all_requested: bool = False,
    limit: int | None = None,
) -> list[str]:
    """按显式代码/类型/全量解析事件采集目标。"""
    explicit = list(
        dict.fromkeys(str(code).strip() for code in (codes or []) if str(code).strip())
    )
    if explicit:
        targets = explicit
    elif all_requested or fund_types:
        targets = (
            resolve_by_types(fund_types or [])
            if fund_types
            else worker_base.resolve_codes([], [])
        )
    else:
        targets = []
    return targets[:limit] if limit is not None else targets


def run_events(
    codes: list[str] | None = None,
    fund_types: list[str] | None = None,
    *,
    all_requested: bool = False,
    concurrency: int = 4,
    limit: int | None = None,
) -> dict:
    """CLI 用批处理入口。"""
    targets = resolve_codes(codes, fund_types, all_requested=all_requested, limit=limit)
    ensure_batch_allowed(len(targets))
    success = fail = rows = 0
    failures = []
    if not targets:
        return {"total": 0, "success": 0, "fail": 0, "rows": 0, "failures": []}
    with ThreadPoolExecutor(
        max_workers=min(64, max(1, int(concurrency))), thread_name_prefix="nav-events"
    ) as executor:
        futures = {executor.submit(sync_one, code): code for code in targets}
        completed = 0
        for future in as_completed(futures):
            code = futures[future]
            try:
                result = future.result()
                success += 1
                rows += int(result.get("rows") or 0)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                fail += 1
                failures.append({"code": code, "error": str(exc)})
            completed += 1
            if completed % CHECKPOINT_EVERY == 0:
                checkpoint_wal()
        if completed and completed % CHECKPOINT_EVERY:
            checkpoint_wal()
    return {
        "total": len(targets),
        "success": success,
        "fail": fail,
        "rows": rows,
        "failures": failures[:20],
    }


if __name__ == "__main__":
    worker_base.main(process_one)
