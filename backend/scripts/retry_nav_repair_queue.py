#!/usr/bin/env python3
"""按错误类型分批重试 nav_repair_queue，或归档确认不可恢复的失败。

默认 dry-run，不写库、不删除行。归档前需先执行
``scripts/apply_nav_repair_archive_ddl.py``，否则 MySQL CHECK 会拒绝
``status='archived'``。

示例（在 backend 目录、已加载 /etc/ifund-prod.env）::

    ./venv/bin/python3 scripts/retry_nav_repair_queue.py
    ./venv/bin/python3 scripts/retry_nav_repair_queue.py --bucket retry_nav --limit 50
    ./venv/bin/python3 scripts/retry_nav_repair_queue.py --apply --bucket retry_nav --limit 50
    ./venv/bin/python3 scripts/retry_nav_repair_queue.py --apply --bucket archive_adj_empty --limit 200
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from app.fund_nav.crud import repair_crud  # noqa: E402 pylint: disable=wrong-import-position
from app.fund_nav.fetch import adj_engine, backfill_worker, events_worker  # noqa: E402 pylint: disable=wrong-import-position
from app.fund_nav.repair_triage import (  # noqa: E402 pylint: disable=wrong-import-position
    ARCHIVE_ADJ_EMPTY,
    RETRY_ADJ,
    RETRY_EVENTS,
    RETRY_NAV,
    REVIEW,
    failure_message,
    group_failures,
    safe_error_text,
    select_batch,
)


def _sample(rows: list[dict], size: int = 5) -> list[dict]:
    sample = []
    for row in rows[:size]:
        sample.append({
            "id": row.get("id"),
            "fund_code": row.get("fund_code"),
            "task_kind": row.get("task_kind"),
            "last_error": str(row.get("last_error") or "")[:180],
        })
    return sample


def _apply_one(row: dict) -> dict:
    """执行一条已分类任务，返回可序列化结果。"""
    task_id = int(row["id"])
    code = str(row.get("fund_code") or "")
    action = str(row["action"])
    base = {"id": task_id, "fund_code": code, "action": action}
    if action == ARCHIVE_ADJ_EMPTY:
        repair_crud.mark_archived(task_id)
        return {**base, "result": "archived"}
    if action == RETRY_EVENTS:
        try:
            events_worker.sync_one(code)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            error = safe_error_text(f"event scan failed: {exc}")
            repair_crud.mark_failed(task_id, error)
            return {**base, "result": "failed", "error": error}
        repair_crud.mark_done(task_id)
        return {**base, "result": "done"}
    if action == RETRY_NAV:
        outcome = backfill_worker.backfill_one(code)
        if outcome.get("status") == "success":
            repair_crud.mark_done(task_id)
            return {**base, "result": "done"}
        error = safe_error_text(outcome.get("error") or outcome.get("reason") or "净值回补失败")
        repair_crud.mark_failed(task_id, error)
        return {**base, "result": "failed", "error": error}
    if action == RETRY_ADJ:
        outcome = adj_engine.fetch_adj_tushare([code])
        if not outcome.get("failed"):
            repair_crud.mark_done(task_id)
            return {**base, "result": "done"}
        error = failure_message(outcome)
        repair_crud.mark_failed(task_id, error)
        return {**base, "result": "failed", "error": error}
    raise RuntimeError(f"拒绝执行 bucket {action}")


def build_report(rows: list[dict], *, bucket: str | None, limit: int, apply: bool) -> dict:
    """生成 dry-run 摘要；apply 为真时附带本批执行结果。"""
    grouped = group_failures(rows)
    report = {
        "dry_run": not apply,
        "total_failed": len(rows),
        "counts": {key: len(value) for key, value in grouped.items()},
        "samples": {key: _sample(value) for key, value in grouped.items() if value},
    }
    if not apply:
        report["note"] = (
            "未写库。确认后使用 --apply --bucket "
            "{retry_nav|retry_events|retry_adj|archive_adj_empty|all_retryable} --limit N"
        )
        if bucket:
            report["planned"] = _sample(select_batch(grouped, bucket, limit), limit)
        return report
    if not bucket:
        raise ValueError("--apply 必须同时指定 --bucket")
    if bucket == REVIEW:
        raise ValueError("review 不自动写库")
    planned = select_batch(grouped, bucket, limit)
    results = [_apply_one(row) for row in planned]
    report["applied"] = len(results)
    report["results"] = results
    report["failed"] = sum(1 for item in results if item.get("result") == "failed")
    return report


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数。默认 dry-run。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="真正写库；省略则只打印计划")
    parser.add_argument(
        "--bucket",
        choices=[RETRY_NAV, RETRY_EVENTS, RETRY_ADJ, ARCHIVE_ADJ_EMPTY, "all_retryable"],
        help="要查看或执行的错误类型",
    )
    parser.add_argument("--limit", type=int, default=50, help="每个桶最多处理条数")
    return parser


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    args = build_parser().parse_args(argv)
    if args.limit < 0:
        print("limit 不能为负", file=sys.stderr)
        return 2
    rows = repair_crud.list_failed()
    try:
        report = build_report(rows, bucket=args.bucket, limit=args.limit, apply=args.apply)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report.get("failed"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
