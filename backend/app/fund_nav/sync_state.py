"""基金同步任务的持久化水位读写。"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping

from app import db as database

TABLE = "fund_sync_state"


def _date_text(value: object) -> str | None:
    """把数据库 DATE 或字符串统一为 ISO 日期。"""
    if value is None or value == "":
        return None
    if isinstance(value, (dt.date, dt.datetime)):
        return (
            value.date().isoformat()
            if isinstance(value, dt.datetime)
            else value.isoformat()
        )
    return dt.date.fromisoformat(str(value)).isoformat()


def get_watermarks(task_kind: str) -> dict[str, str]:
    """一次查询返回指定任务的全部有效基金水位。"""
    rows = database.select(
        TABLE,
        [
            ("task_kind", f"eq.{task_kind}"),
            ("select", "fund_code,watermark_date"),
            ("limit", 1_000_000),
        ],
    )
    watermarks: dict[str, str] = {}
    for row in rows:
        code = str(row.get("fund_code") or "").strip()
        watermark = _date_text(row.get("watermark_date"))
        if code and watermark:
            watermarks[code] = watermark
    return watermarks


def set_watermarks(task_kind: str, watermarks: Mapping[str, object]) -> int:
    """批量幂等推进水位，返回提交的基金数。"""
    rows = []
    for fund_code, value in watermarks.items():
        code = str(fund_code or "").strip()
        watermark = _date_text(value)
        if code and watermark:
            rows.append(
                {
                    "fund_code": code,
                    "task_kind": task_kind,
                    "watermark_date": watermark,
                    "status": "success",
                    "attempts": 0,
                    "last_error": None,
                }
            )
    if rows:
        database.batch_insert(TABLE, rows)
    return len(rows)


def set_watermark(fund_code: str, task_kind: str, watermark_date: object) -> None:
    """幂等推进单只基金水位。"""
    set_watermarks(task_kind, {fund_code: watermark_date})


def mark_empty(fund_code: str, task_kind: str) -> None:
    """预留：记录远端确认无数据，不覆盖已有水位。"""
    database.batch_insert(
        TABLE,
        [
            {
                "fund_code": fund_code,
                "task_kind": task_kind,
                "status": "empty",
                "attempts": 0,
                "last_error": None,
            }
        ],
    )


def mark_failed(
    fund_code: str,
    task_kind: str,
    error: object,
    *,
    attempts: int = 1,
) -> None:
    """预留：记录同步失败，不覆盖已有水位。"""
    database.batch_insert(
        TABLE,
        [
            {
                "fund_code": fund_code,
                "task_kind": task_kind,
                "status": "failed",
                "attempts": max(1, int(attempts)),
                "last_error": str(error),
            }
        ],
    )
