#!/usr/bin/env python3
"""iFund 全量补充同步：行业映射、代表基金净值/持仓、批量基金详情。"""

from __future__ import annotations

import datetime
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_DIR / "backend"
ERROR_LOG = PROJECT_DIR / "logs" / "sync_errors.log"
FUND_TIMEOUT_SEC = 10
DETAIL_LIMIT = 120
REPRESENTATIVE_CODES = [
    "000051", "510300", "510050", "159915", "110022", "000001",
    "161725", "003096", "005827", "007300", "163406", "001186",
]

os.environ.setdefault("IFUND_BACKEND_DIR", str(BACKEND_DIR))
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)
load_dotenv(BACKEND_DIR / ".env")

# pylint: disable=wrong-import-position,protected-access
from app import db as database
from app.fund_detail.fetch.worker import _process_one as process_detail
from app.fund_holdings.fetch.worker import _process_one as process_holdings
from app.fund_nav.fetch.worker import _process_one as process_nav
from app.stock_industry.fetch import em_worker, sw_worker


class FundCallTimeout(BaseException):
    """绕过 worker 内部 broad Exception，确保单基金调用能在 10 秒硬停止。"""


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _elapsed(started: float) -> str:
    seconds = max(0, int(time.monotonic() - started))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _print(message: str) -> None:
    print(f"[{_now()}] {message}", flush=True)


def _log_error(stage: str, item: str, detail: str) -> None:
    ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ERROR_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{_now()}\t{stage}\t{item}\t{detail}\n")


def _alarm_handler(_signum, _frame) -> None:
    raise FundCallTimeout(f"超过 {FUND_TIMEOUT_SEC} 秒")


def _call_fund(stage: str, code: str, func: Callable[[str], str]) -> tuple[str, float]:
    """执行单基金同步；返回 success/skip/fail，超时和异常均落日志。"""
    started = time.monotonic()
    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _alarm_handler)
    signal.setitimer(signal.ITIMER_REAL, FUND_TIMEOUT_SEC)
    try:
        result = func(code) or "success"
        if result == "fail":
            _log_error(stage, code, "worker 返回 fail")
        return result, time.monotonic() - started
    except FundCallTimeout as exc:
        _log_error(stage, code, f"timeout: {exc}")
        return "fail", time.monotonic() - started
    except Exception as exc:  # pylint: disable=broad-exception-caught
        _log_error(stage, code, f"{type(exc).__name__}: {exc}")
        return "fail", time.monotonic() - started
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def _new_task(task_type: str) -> int:
    task = database.insert("fetch_tasks", {
        "task_type": task_type,
        "status": "running",
        "executor_ip": "bgjob",
        "executor_thread": threading.current_thread().name,
    })
    return int(task["id"])


def _industry_stage(
    stage_no: int,
    label: str,
    task_type: str,
    runner: Callable[[int], None],
) -> dict:
    """在线程中运行行业 worker，主线程轮询任务表并输出可见进度。"""
    started = time.monotonic()
    _print(f"阶段{stage_no}开始：{label}")
    try:
        task_id = _new_task(task_type)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        _log_error(f"stage{stage_no}", task_type, f"创建任务失败: {type(exc).__name__}: {exc}")
        _print(f"阶段{stage_no}无法创建任务，已记录并继续：{exc}")
        return {"status": "failed", "fail_count": 1}

    failure: list[BaseException] = []

    def _target() -> None:
        try:
            runner(task_id)
        except BaseException as exc:  # 保证线程异常能回传并将任务收口
            failure.append(exc)
            database.update("fetch_tasks", {"id": task_id}, {"status": "terminated"})

    worker = threading.Thread(target=_target, name=f"sync-stage-{stage_no}")
    worker.start()
    last_current = -1
    last_report = 0.0
    while worker.is_alive():
        worker.join(timeout=5)
        row = database.select_one("fetch_tasks", {"id": f"eq.{task_id}"}) or {}
        current = int(row.get("current_count") or 0)
        now = time.monotonic()
        if current != last_current or now - last_report >= 30:
            _print(
                f"阶段{stage_no}进度：{current}/{int(row.get('target_count') or 0)}，"
                f"耗时 {_elapsed(started)}，失败 {int(row.get('fail_count') or 0)}"
            )
            last_current = current
            last_report = now

    row = database.select_one("fetch_tasks", {"id": f"eq.{task_id}"}) or {}
    if failure:
        exc = failure[0]
        _log_error(f"stage{stage_no}", label, f"{type(exc).__name__}: {exc}")
    _print(
        f"阶段{stage_no}完成：状态 {row.get('status', 'unknown')}，"
        f"{int(row.get('current_count') or 0)}/{int(row.get('target_count') or 0)}，"
        f"耗时 {_elapsed(started)}，失败 {int(row.get('fail_count') or 0)}"
    )
    return row


def _representative_stage() -> None:
    """同步至少 10 只代表基金的净值和持仓。"""
    started = time.monotonic()
    funds = {row["code"] for row in database.select("funds", {"select": "code"})}
    targets = [code for code in REPRESENTATIVE_CODES if code in funds]
    _print(f"阶段3开始：代表基金净值+持仓，共 {len(targets)} 只")
    failures = 0
    skips = 0
    for index, code in enumerate(targets, 1):
        nav_status, nav_seconds = _call_fund("stage3-nav", code, process_nav)
        holdings_status, holdings_seconds = _call_fund("stage3-holdings", code, process_holdings)
        failures += int(nav_status == "fail") + int(holdings_status == "fail")
        skips += int(nav_status == "skip") + int(holdings_status == "skip")
        _print(
            f"阶段3进度：{index}/{len(targets)}，基金 {code}，"
            f"净值 {nav_status}({nav_seconds:.1f}s)，持仓 {holdings_status}({holdings_seconds:.1f}s)，"
            f"耗时 {_elapsed(started)}，失败 {failures}，跳过 {skips}"
        )
    _print(
        f"阶段3完成：{len(targets)}/{len(targets)}，耗时 {_elapsed(started)}，"
        f"失败 {failures}，跳过 {skips}"
    )


def _detail_targets() -> list[str]:
    rows = database.select("funds", {"order": "code.asc"})
    targets = []
    for row in rows:
        fund_type = str(row.get("type") or "")
        if (
            fund_type.startswith("股票型")
            or fund_type.startswith("混合型-偏股")
            or fund_type.startswith("指数型")
        ):
            targets.append(str(row["code"]))
        if len(targets) >= DETAIL_LIMIT:
            break
    return targets


def _detail_stage() -> None:
    """从目标基金类型中稳定选择前 120 只，逐只补充详情。"""
    started = time.monotonic()
    targets = _detail_targets()
    _print(f"阶段4开始：股票型/偏股混合/指数型基金详情，共 {len(targets)} 只")
    failures = 0
    skips = 0
    for index, code in enumerate(targets, 1):
        status, seconds = _call_fund("stage4-detail", code, process_detail)
        failures += int(status == "fail")
        skips += int(status == "skip")
        _print(
            f"阶段4进度：{index}/{len(targets)}，基金 {code}，{status}({seconds:.1f}s)，"
            f"耗时 {_elapsed(started)}，失败 {failures}，跳过 {skips}"
        )
    _print(
        f"阶段4完成：{len(targets)}/{len(targets)}，耗时 {_elapsed(started)}，"
        f"失败 {failures}，跳过 {skips}"
    )


def _print_table_counts() -> None:
    tables = [
        "funds",
        "trade_dates",
        "stock_industry",
        "fund_nav",
        "fund_cum_return",
        "fund_holdings",
        "fund_details",
        "fetch_tasks",
    ]
    _print("最终数据表计数汇总：")
    for table in tables:
        try:
            _print(f"  {table}: {database.count(table):,}")
        except Exception as exc:  # pylint: disable=broad-exception-caught
            _log_error("summary", table, f"{type(exc).__name__}: {exc}")
            _print(f"  {table}: 统计失败（已记录）")


def main() -> int:
    total_started = time.monotonic()
    ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
    _print(
        f"iFund 同步启动：单基金超时 {FUND_TIMEOUT_SEC}s，"
        f"代表基金 {len(REPRESENTATIVE_CODES)} 只，详情上限 {DETAIL_LIMIT} 只"
    )
    _industry_stage(
        1,
        "申万三级行业映射（完整目录/续采模式）",
        "ifund_sync_sw_industry",
        lambda task_id: sw_worker.run(task_id, []),
    )
    _industry_stage(
        2,
        "东财行业补充",
        "ifund_sync_em_industry",
        em_worker.run,
    )
    _representative_stage()
    _detail_stage()
    _print_table_counts()
    _print(f"iFund 全部同步阶段结束，总耗时 {_elapsed(total_started)}；失败日志：{ERROR_LOG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
