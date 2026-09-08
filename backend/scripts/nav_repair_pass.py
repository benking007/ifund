#!/usr/bin/env python3
"""净值治理失败队列晚间重试。

建议 crontab（仅注释，不由本脚本安装）：
``30 21 * * * cd /root/workspace/ifund/backend && ./venv/bin/python3.12 scripts/nav_repair_pass.py``
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)

from app.fund_nav.crud import repair_crud  # noqa: E402  pylint: disable=wrong-import-position
from app.fund_nav.fetch import adj_engine, backfill_worker  # noqa: E402 pylint: disable=wrong-import-position


logger = logging.getLogger("nav_repair_pass")
MAX_TASKS = 50


def _run_task(task: dict) -> tuple[dict, bool, str]:
    """执行一条队列任务，返回任务、成功标识和错误文本。"""
    task_id = int(task["id"])
    repair_crud.mark_running(task_id)
    try:
        code = task["fund_code"]
        if task["task_kind"] == "nav":
            result = backfill_worker.backfill_one(code)
            ok = result.get("status") == "success"
            error = result.get("error", "")
        else:
            result = adj_engine.fetch_adj_tushare([code])
            ok = not result.get("failed")
            error = ";".join(result.get("failed") or [])
        if ok:
            repair_crud.mark_done(task_id)
            return task, True, ""
        repair_crud.mark_failed(task_id, error or "治理任务失败")
        return task, False, error
    except Exception as exc:  # pylint: disable=broad-exception-caught
        repair_crud.mark_failed(task_id, str(exc))
        return task, False, str(exc)


def run_repair_pass(limit: int = MAX_TASKS, concurrency: int = 4) -> dict:
    """重试到期的 pending/failed 任务；单次最多处理 50 条。"""
    limit = min(MAX_TASKS, max(0, int(limit)))
    tasks = repair_crud.list_retryable(limit=limit, now=dt.datetime.now().isoformat(timespec="seconds"))
    backfill_worker.ensure_batch_allowed(len(tasks))
    for task in tasks:
        if int(task.get("attempts") or 0) >= 3:
            logger.warning(
                "基金 %s 的 %s repair attempts=%s，连续失败达到 3 次",
                task.get("fund_code"), task.get("task_kind"), task.get("attempts"),
            )
    success = fail = 0
    failures = []
    if tasks:
        with ThreadPoolExecutor(max_workers=min(64, max(1, int(concurrency))),
                                thread_name_prefix="nav-repair") as executor:
            futures = [executor.submit(_run_task, task) for task in tasks]
            for future in as_completed(futures):
                task, ok, error = future.result()
                if ok:
                    success += 1
                else:
                    fail += 1
                    failures.append({"code": task.get("fund_code"), "error": error})
    return {"total": len(tasks), "success": success, "fail": fail, "failures": failures[:20]}


def build_parser() -> argparse.ArgumentParser:
    """构造 repair pass 命令行参数。"""
    parser = argparse.ArgumentParser(description="重试到期的净值治理队列")
    parser.add_argument("--limit", type=int, default=MAX_TASKS)
    parser.add_argument("--concurrency", type=int, default=4)
    return parser


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = run_repair_pass(args.limit, args.concurrency)
    print(result)
    return 0 if result["fail"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
