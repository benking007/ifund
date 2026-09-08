#!/usr/bin/env python3
"""基金单位净值全量回补 worker。"""
from __future__ import annotations

import datetime as dt
import fnmatch
import logging
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_BACKEND_DIR = os.getenv("IFUND_BACKEND_DIR") or str(Path(__file__).resolve().parents[3])
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)
os.chdir(_BACKEND_DIR)

# pylint: disable=wrong-import-position
from app import db as database
from app.common import worker_base
from app.common.network import MAX_NETWORK_ATTEMPTS, is_retryable_network_error
from app.fund_nav.crud import nav_crud, repair_crud
from app.fund_nav.fetch import eastmoney, no_nav_blacklist
from app.fund_nav.fetch.errors import F10_EMPTY, NoNavDataError, is_no_nav_data_error, reason_for


logger = logging.getLogger(__name__)
REQUEST_INTERVAL_SECONDS = 0.3
BACKOFF_BASE_SECONDS = 2
RETRY_JITTER = 0.2
MAX_NIGHT_BATCH = 50
CHECKPOINT_EVERY = 100


class _RateLimiter:
    """跨线程保证基金之间至少间隔指定时间。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_at: float | None = None

    def reset(self) -> None:
        """测试/进程重启时清空节流状态。"""
        with self._lock:
            self._next_at = None

    def wait(self, interval: float = REQUEST_INTERVAL_SECONDS) -> None:
        """等待并预留一个请求时间槽。"""
        with self._lock:
            now = time.monotonic()
            wait_seconds = max(0.0, (self._next_at or now) - now)
            self._next_at = max(now, self._next_at or now) + interval
        if wait_seconds:
            time.sleep(wait_seconds)


_rate_limiter = _RateLimiter()


def wait_for_slot(interval: float = REQUEST_INTERVAL_SECONDS) -> None:
    """等待东财请求限速槽位。"""
    _rate_limiter.wait(interval)


def checkpoint_wal() -> None:
    """在长批处理边界尝试截断 WAL；非 SQLite 后端则静默跳过。"""
    checkpoint = getattr(database.get_db(), "checkpoint", None)
    if callable(checkpoint):
        checkpoint()


def reset_rate_limiter() -> None:
    """重置东财请求限速器，供测试和进程内重新开始一轮任务使用。"""
    _rate_limiter.reset()


def ensure_batch_allowed(count: int, now: dt.datetime | None = None) -> None:
    """工作日 20:00–23:00 禁止超过 50 只基金的治理批量拉取。"""
    current = now or dt.datetime.now()
    if os.getenv("IFUND_BYPASS_TIME_GATE") == "1":
        logger.warning("time gate bypassed by env")
        return
    if current.date().weekday() >= 5:
        return
    if count > MAX_NIGHT_BATCH and 20 <= current.hour < 23:
        raise RuntimeError("20:00–23:00 单批最多处理 50 只基金")


def _normalise_rows(code: str, rows: list[dict]) -> list[dict]:
    now = dt.datetime.now().isoformat(timespec="seconds")
    result = []
    for row in rows:
        day = str(row.get("trade_date") or "").strip()
        if not day:
            continue
        result.append({
            "fund_code": code,
            "trade_date": day,
            "nav": row.get("nav"),
            "acc_nav": row.get("acc_nav"),
            "daily_return": row.get("daily_return"),
            "fetch_time": now,
        })
    return result


def _failure_gap_end() -> str:
    return dt.date.today().isoformat()


def backfill_one(code: str) -> dict:
    """拉取单只基金全史；仅网络错误重试，无净值则持久化 skip。"""
    if no_nav_blacklist.is_blacklisted(code):
        return {"fund_code": code, "status": "skip", "rows": 0, "reason": "blacklisted"}
    last_error = ""
    for attempt in range(MAX_NETWORK_ATTEMPTS):
        try:
            wait_for_slot()
            source_rows = eastmoney.fetch_nav_full(code)
            if not source_rows:
                raise NoNavDataError(f"基金 {code} F10 全史为空", reason=F10_EMPTY)
            rows = _normalise_rows(code, source_rows)
            written = nav_crud.upsert_nav_rows(rows)
            repair_crud.mark_done_for_fund(code, "nav")
            return {"fund_code": code, "status": "success", "rows": written}
        except Exception as exc:  # pylint: disable=broad-exception-caught
            last_error = str(exc)
            if is_no_nav_data_error(exc):
                logger.warning("基金 %s 东财单源无适用单位净值，不写黑名单：%s", code, exc)
                return {"fund_code": code, "status": "skip", "rows": 0, "reason": reason_for(exc)}
            retryable = is_retryable_network_error(exc)
            if not retryable or attempt + 1 >= MAX_NETWORK_ATTEMPTS:
                attempts = attempt + 1
                logger.warning("基金 %s 全量回补失败（已尝试 %d 次）：%s", code, attempts, exc)
                repair_crud.record_failure(
                    code, "nav", last_error,
                    gap_start="2000-01-01", gap_end=_failure_gap_end(), attempts=attempts,
                )
                return {"fund_code": code, "status": "fail", "rows": 0, "error": last_error}
            delay = (BACKOFF_BASE_SECONDS ** attempt) * random.uniform(
                1 - RETRY_JITTER, 1 + RETRY_JITTER,
            )
            logger.warning("基金 %s 网络失败，第 %d 次重试，等待 %.2f 秒：%s", code, attempt + 1, delay, exc)
            time.sleep(delay)
    return {"fund_code": code, "status": "fail", "rows": 0, "error": last_error}


def process_one(code: str) -> str:
    """worker_base 所需的单基金状态回调。"""
    return backfill_one(code)["status"]


def resolve_by_types(fund_types: list[str]) -> list[str]:
    """按实际类型名称匹配通配符，支持 ``货币型*``。"""
    rows = database.select("funds", [("select", "code,type"), ("order", "code.asc")])
    selectors = [str(item).strip() for item in fund_types if str(item).strip()]
    return [
        row["code"] for row in rows
        if any(fnmatch.fnmatchcase(str(row.get("type") or ""), selector)
               for selector in selectors)
    ]


def resolve_codes(
    codes: list[str] | None = None,
    fund_types: list[str] | None = None,
    *,
    all_requested: bool = False,
    limit: int | None = None,
    task_kind: str = "nav",
) -> list[str]:
    """解析显式代码、类型全集或默认的 pending 队列。"""
    explicit = list(dict.fromkeys(str(code).strip() for code in (codes or []) if str(code).strip()))
    if explicit:
        targets = explicit
    elif all_requested or fund_types:
        targets = resolve_by_types(fund_types or []) if fund_types else worker_base.resolve_codes([], [])
    else:
        targets = [
            row["fund_code"]
            for row in repair_crud.list_pending(limit=100000, task_kind=task_kind)
        ]
    blocked = no_nav_blacklist.blacklisted_codes()
    targets = [code for code in dict.fromkeys(targets) if code not in blocked]
    return targets[:limit] if limit is not None else targets


def run_backfill(
    codes: list[str] | None = None,
    fund_types: list[str] | None = None,
    *,
    all_requested: bool = False,
    concurrency: int = 8,
    limit: int | None = None,
) -> dict:
    """CLI 用线程池批处理，返回可序列化汇总。"""
    targets = resolve_codes(codes, fund_types, all_requested=all_requested, limit=limit)
    ensure_batch_allowed(len(targets))
    concurrency = min(64, max(1, int(concurrency)))
    success = fail = skipped = rows = 0
    failures = []
    if not targets:
        return {"total": 0, "success": 0, "fail": 0, "skip": 0, "rows": 0, "failures": []}
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="nav-backfill") as executor:
        futures = {executor.submit(backfill_one, code): code for code in targets}
        completed = 0
        for future in as_completed(futures):
            result = future.result()
            if result["status"] == "success":
                success += 1
                rows += int(result.get("rows") or 0)
            elif result["status"] == "skip":
                skipped += 1
            else:
                fail += 1
                failures.append({"code": result["fund_code"], "error": result.get("error", "")})
            completed += 1
            if completed % CHECKPOINT_EVERY == 0:
                checkpoint_wal()
        if completed and completed % CHECKPOINT_EVERY:
            checkpoint_wal()
    return {
        "total": len(targets), "success": success, "fail": fail, "skip": skipped,
        "rows": rows, "failures": failures[:20],
    }


def _standalone_argv() -> list[str]:
    """把 backfill 专用参数转换成 worker_base 可识别的参数。"""
    argv = sys.argv[1:]
    all_requested = "--all" in argv
    filtered = [item for item in argv if item != "--all"]
    concurrency = None
    output = []
    index = 0
    while index < len(filtered):
        item = filtered[index]
        if item == "--concurrency" and index + 1 < len(filtered):
            concurrency = filtered[index + 1]
            index += 2
            continue
        output.append(item)
        index += 1
    if concurrency:
        os.environ["IFUND_WORKER_CONCURRENCY"] = concurrency
    has_selector = any(item in output for item in ("--codes", "--fund-types", "--types"))
    if not has_selector and not all_requested:
        pending = resolve_codes()
        if not pending:
            return []
        output.extend(["--codes", *pending])
    return output


if __name__ == "__main__":
    _all_requested = "--all" in sys.argv[1:]
    standalone = _standalone_argv()
    if standalone or _all_requested:
        sys.argv[1:] = standalone
        worker_base.main(process_one)
    else:
        logger.info("没有待处理的 nav_repair_queue 任务")
