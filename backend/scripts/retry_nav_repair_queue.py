#!/usr/bin/env python3
"""按错误类型分批重试 nav_repair_queue，或归档确认不可恢复的失败。

默认 dry-run，不写库、不删除行。``retry_nav`` 优先按队列 ``gap_start``/``gap_end``
合并相邻缺口，只向 Tushare ``fund_nav`` 要该窗口；无法定窗的行仍走全史回补。
``--stop-at HH:MM`` 使用 Asia/Shanghai，长跑在下一次到达该时刻时停止新请求。
归档前需先执行 ``scripts/apply_nav_repair_archive_ddl.py``，否则 MySQL CHECK
会拒绝 ``status='archived'``。

示例（在 backend 目录、已加载 /etc/ifund-prod.env）::

    ./venv/bin/python3 scripts/retry_nav_repair_queue.py
    ./venv/bin/python3 scripts/retry_nav_repair_queue.py --bucket retry_nav --limit 50
    ./venv/bin/python3 scripts/retry_nav_repair_queue.py --apply --bucket retry_nav --limit 50
    ./venv/bin/python3 scripts/retry_nav_repair_queue.py --apply --bucket retry_nav --limit 50 --stop-at 07:50
    ./venv/bin/python3 scripts/retry_nav_repair_queue.py --apply --bucket archive_adj_empty --limit 200
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from app.fund_nav.crud import repair_crud  # noqa: E402 pylint: disable=wrong-import-position
from app.fund_nav.fetch import adj_engine, events_worker  # noqa: E402 pylint: disable=wrong-import-position
from app.fund_nav.nav_window_retry import (  # noqa: E402 pylint: disable=wrong-import-position
    execute_nav_batch,
    now_shanghai,
    plan_summary,
    resolve_deadline,
    should_stop,
)
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
        # 单行入口仍走窗口规划；批处理在 build_report 里按基金合并，避免这里拆开。
        batch = execute_nav_batch([row])
        return batch["results"][0]
    if action == RETRY_ADJ:
        outcome = adj_engine.fetch_adj_tushare([code])
        if not outcome.get("failed"):
            repair_crud.mark_done(task_id)
            return {**base, "result": "done"}
        error = failure_message(outcome)
        repair_crud.mark_failed(task_id, error)
        return {**base, "result": "failed", "error": error}
    raise RuntimeError(f"拒绝执行 bucket {action}")


def _nav_rows_in_scope(grouped: dict, bucket: str | None, limit: int) -> list[dict]:
    """本命令会看到的 retry_nav 行。limit 先截断，再按基金合并窗口。"""
    if bucket in (RETRY_NAV, "all_retryable"):
        chosen = select_batch(grouped, bucket, limit)
        return [row for row in chosen if row.get("action") == RETRY_NAV]
    if bucket is None:
        return list(grouped.get(RETRY_NAV, []))
    return []


def _stopped_row(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "fund_code": str(row.get("fund_code") or ""),
        "action": row.get("action"),
        "result": "stopped",
    }


def _apply_planned(
    planned: list[dict],
    *,
    stop_at: dt.time | None,
    max_minutes: float | None,
    clock,
    started: dt.datetime,
) -> tuple[list[dict], dict]:
    """retry_nav 按窗口批处理；其余桶保持逐行。到点后不再开始新请求。"""
    nav_rows = [row for row in planned if row.get("action") == RETRY_NAV]
    others = [row for row in planned if row.get("action") != RETRY_NAV]
    nav_report = execute_nav_batch(
        nav_rows,
        stop_at=stop_at,
        max_minutes=max_minutes,
        clock=clock,
        started=started,
    )
    results = list(nav_report["results"])
    if nav_report["stopped_rows"]:
        results.extend(_stopped_row(row) for row in others)
        return results, nav_report
    deadline = resolve_deadline(started, stop_at)
    for index, row in enumerate(others):
        if should_stop(clock(), started=started, deadline=deadline, max_minutes=max_minutes):
            results.extend(_stopped_row(rest) for rest in others[index:])
            break
        results.append(_apply_one(row))
    return results, nav_report


def build_report(
    rows: list[dict],
    *,
    bucket: str | None,
    limit: int,
    apply: bool,
    stop_at: dt.time | None = None,
    max_minutes: float | None = None,
    clock=None,
) -> dict:
    """生成 dry-run 摘要；apply 为真时附带本批执行结果。"""
    grouped = group_failures(rows)
    report = {
        "dry_run": not apply,
        "total_failed": len(rows),
        "counts": {key: len(value) for key, value in grouped.items()},
        "samples": {key: _sample(value) for key, value in grouped.items() if value},
    }
    scope = _nav_rows_in_scope(grouped, bucket, limit)
    if not apply:
        report["nav_retry"] = plan_summary(scope)
        report["note"] = (
            "未写库。确认后使用 --apply --bucket "
            "{retry_nav|retry_events|retry_adj|archive_adj_empty|all_retryable} --limit N "
            "[--stop-at HH:MM | --max-minutes M]"
        )
        if bucket:
            report["planned"] = _sample(select_batch(grouped, bucket, limit), limit)
        return report
    if not bucket:
        raise ValueError("--apply 必须同时指定 --bucket")
    if bucket == REVIEW:
        raise ValueError("review 不自动写库")
    tick = clock or now_shanghai
    started = tick()
    planned = select_batch(grouped, bucket, limit)
    results, nav_report = _apply_planned(
        planned,
        stop_at=stop_at,
        max_minutes=max_minutes,
        clock=tick,
        started=started,
    )
    public_nav = {key: value for key, value in nav_report.items() if key != "results"}
    report["nav_retry"] = public_nav
    report["applied"] = sum(1 for item in results if item.get("result") != "stopped")
    report["stopped"] = sum(1 for item in results if item.get("result") == "stopped")
    report["results"] = results
    report["failed"] = sum(1 for item in results if item.get("result") == "failed")
    return report


def _parse_stop_at(value: str) -> dt.time:
    """解析 Asia/Shanghai 的 HH:MM 截止时刻。"""
    parts = value.split(":")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("stop-at 必须为 HH:MM")
    try:
        parsed = dt.time(int(parts[0]), int(parts[1]))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("stop-at 必须为 HH:MM") from exc
    return parsed


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
    parser.add_argument(
        "--max-minutes",
        type=float,
        default=None,
        help="最长运行分钟数，到点后不再开始新请求",
    )
    parser.add_argument(
        "--stop-at",
        type=_parse_stop_at,
        default=None,
        help="Asia/Shanghai 时刻 HH:MM，长跑在下一次到达该时刻时停止（如 07:50）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.limit < 0:
        print("limit 不能为负", file=sys.stderr)
        return 2
    if args.max_minutes is not None and args.max_minutes < 0:
        print("max-minutes 不能为负", file=sys.stderr)
        return 2
    rows = repair_crud.list_failed()
    try:
        report = build_report(
            rows,
            bucket=args.bucket,
            limit=args.limit,
            apply=args.apply,
            stop_at=args.stop_at,
            max_minutes=args.max_minutes,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report.get("failed"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
