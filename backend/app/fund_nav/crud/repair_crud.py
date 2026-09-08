"""净值治理缺口队列 CRUD。"""
from __future__ import annotations

import datetime as dt

from app import db as database


TABLE = "nav_repair_queue"
DEFAULT_GAP_START = "2000-01-01"
DEFAULT_GAP_END = "2999-12-31"


def _now() -> str:
    """返回本地 ISO 时间，和现有 fetch_tasks 时间口径一致。"""
    return dt.datetime.now().isoformat(timespec="seconds")


def _retry_at(attempts: int, now: str | None = None) -> str:
    """按尝试次数计算下次重试时间，最大退避 30 分钟。"""
    base = dt.datetime.fromisoformat(now) if now else dt.datetime.now()
    delay = min(30 * 60, 2 ** max(0, attempts - 1))
    return (base + dt.timedelta(seconds=delay)).isoformat(timespec="seconds")


def enqueue(
    fund_code: str,
    task_kind: str,
    gap_start: str | None = None,
    gap_end: str | None = None,
    *,
    status: str = "pending",
    attempts: int = 0,
    next_retry_at: str | None = None,
    last_error: str | None = None,
) -> dict:
    """新增或更新一条缺口任务，重复调用保持幂等。"""
    # SQLite UNIQUE 对 NULL 不去重；缺口未指定时使用稳定的全史哨兵范围，
    # 让重复入队仍然命中同一业务键。
    gap_start = gap_start or DEFAULT_GAP_START
    gap_end = gap_end or DEFAULT_GAP_END
    now = _now()
    filters = {
        "fund_code": f"eq.{fund_code}",
        "task_kind": f"eq.{task_kind}",
        "gap_start": f"eq.{gap_start}" if gap_start is not None else "eq.None",
        "gap_end": f"eq.{gap_end}" if gap_end is not None else "eq.None",
    }
    existing = database.select_one(TABLE, filters)
    fields = {
        "status": status,
        "attempts": attempts,
        "next_retry_at": next_retry_at or now,
        "last_error": last_error,
        "updated_at": now,
    }
    if existing:
        database.update(TABLE, {"id": existing["id"]}, fields)
        return {**existing, **fields}
    return database.insert(TABLE, {
        "fund_code": fund_code,
        "task_kind": task_kind,
        "gap_start": gap_start,
        "gap_end": gap_end,
        "created_at": now,
        **fields,
    })


def list_pending(limit: int = 50, now: str | None = None,
                 task_kind: str | None = None) -> list[dict]:
    """读取待处理任务，不包含已失败任务。"""
    params: list[tuple[str, str] | tuple[str, int]] = [
        ("status", "eq.pending"),
        ("next_retry_at", f"lte.{now or _now()}"),
        ("order", "next_retry_at.asc,id.asc"),
        ("limit", limit),
    ]
    if task_kind:
        params.insert(1, ("task_kind", f"eq.{task_kind}"))
    return database.select(TABLE, params)


def list_retryable(limit: int = 50, now: str | None = None,
                   task_kind: str | None = None) -> list[dict]:
    """读取 pending/failed 且到达重试时间的任务。"""
    params: list[tuple[str, str] | tuple[str, int]] = [
        ("status", "in.(pending,failed)"),
        ("next_retry_at", f"lte.{now or _now()}"),
        ("order", "next_retry_at.asc,id.asc"),
        ("limit", limit),
    ]
    if task_kind:
        params.insert(1, ("task_kind", f"eq.{task_kind}"))
    return database.select(TABLE, params)


def get(task_id: int) -> dict | None:
    """按 id 查询任务。"""
    return database.select_one(TABLE, {"id": f"eq.{task_id}"})


def mark_running(task_id: int) -> None:
    """把任务标成 running。"""
    database.update(TABLE, {"id": task_id}, {
        "status": "running", "updated_at": _now(),
    })


def mark_done(task_id: int) -> None:
    """把任务标成 done 并清空错误。"""
    database.update(TABLE, {"id": task_id}, {
        "status": "done", "last_error": None, "next_retry_at": None,
        "updated_at": _now(),
    })


def mark_failed(
    task_id: int,
    error: str,
    *,
    attempts: int | None = None,
    next_retry_at: str | None = None,
) -> None:
    """写入失败原因和退避时间。"""
    current = get(task_id) or {}
    count = attempts if attempts is not None else int(current.get("attempts") or 0) + 1
    database.update(TABLE, {"id": task_id}, {
        "status": "failed",
        "attempts": count,
        "next_retry_at": next_retry_at or _retry_at(count),
        "last_error": str(error)[:2000],
        "updated_at": _now(),
    })


def record_failure(
    fund_code: str,
    task_kind: str,
    error: str,
    *,
    gap_start: str = "2000-01-01",
    gap_end: str | None = None,
    attempts: int = 3,
) -> dict:
    """为没有队列记录的失败基金创建失败任务，或更新已有记录。"""
    gap_end = gap_end or dt.date.today().isoformat()
    existing = database.select_one(TABLE, {
        "fund_code": f"eq.{fund_code}",
        "task_kind": f"eq.{task_kind}",
        "gap_start": f"eq.{gap_start}",
        "gap_end": f"eq.{gap_end}",
    })
    if existing:
        attempts = max(attempts, int(existing.get("attempts") or 0) + 1)
    return enqueue(
        fund_code,
        task_kind,
        gap_start,
        gap_end,
        status="failed",
        attempts=attempts,
        next_retry_at=_retry_at(attempts),
        last_error=str(error)[:2000],
    )


def mark_done_for_fund(fund_code: str, task_kind: str) -> int:
    """将该基金同类的 pending/running/failed 任务全部收口为 done。"""
    with database.get_db().transaction():
        rows = database.select(TABLE, [
            ("fund_code", f"eq.{fund_code}"),
            ("task_kind", f"eq.{task_kind}"),
            ("status", "in.(pending,running,failed)"),
            ("select", "id"),
        ])
        for row in rows:
            mark_done(int(row["id"]))
        return len(rows)
