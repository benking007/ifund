"""基金分红/拆分事件 CRUD。"""
from __future__ import annotations

import datetime as dt

from app import db as database


TABLE = "fund_div_split"


def _normalise_date(value: object) -> str | None:
    """把 Tushare YYYYMMDD 和本地 YYYY-MM-DD 统一。"""
    if value is None:
        return None
    value = str(value).strip()
    if len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return value or None


def list_events(
        code: str, *, event_type: str | None = None,
        start_date: str | None = None, end_date: str | None = None) -> list[dict]:
    """按基金和除权日升序读取事件。"""
    params: list[tuple[str, str]] = [
        ("fund_code", f"eq.{code}"),
        ("order", "ex_date.asc,event_type.asc"),
    ]
    if event_type:
        params.append(("event_type", f"eq.{event_type}"))
    if start_date:
        params.append(("ex_date", f"gte.{start_date}"))
    if end_date:
        params.append(("ex_date", f"lte.{end_date}"))
    return database.select(TABLE, params)


def _key(row: dict) -> tuple[str, str, str]:
    return (
        str(row.get("fund_code") or ""),
        str(_normalise_date(row.get("ex_date")) or ""),
        str(row.get("event_type") or ""),
    )


def upsert_events(rows: list[dict]) -> int:
    """按基金分批幂等写入事件，返回写入行数。"""
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        code = str(row.get("fund_code") or "").strip()
        ex_date = _normalise_date(row.get("ex_date"))
        event_type = str(row.get("event_type") or "").strip()
        if not code or not ex_date or event_type not in {"div", "split"}:
            continue
        item = {
            "fund_code": code,
            "ex_date": ex_date,
            "event_type": event_type,
            "cash_per_unit": row.get("cash_per_unit"),
            "split_ratio": row.get("split_ratio"),
            "source": row.get("source") or "tushare",
            "fetch_time": row.get("fetch_time") or dt.datetime.now().isoformat(),
        }
        for extra in ("ts_code", "ann_date", "pay_date", "record_date"):
            if row.get(extra) is not None:
                item[extra] = row.get(extra)
        grouped.setdefault(code, []).append(item)
    written = 0
    for code, code_rows in grouped.items():
        with database.get_db().transaction():
            database.batch_insert(TABLE, code_rows)
        written += len(code_rows)
    return written


def upsert_events_changed(rows: list[dict]) -> tuple[int, bool]:
    """写入事件并返回 ``(行数, 是否新增或内容发生变化)``。"""
    grouped_codes = sorted({str(row.get("fund_code") or "") for row in rows if row.get("fund_code")})
    before: dict[tuple[str, str, str], dict] = {}
    for code in grouped_codes:
        for row in list_events(code):
            before[_key(row)] = row
    prepared = []
    for row in rows:
        item = dict(row)
        item["ex_date"] = _normalise_date(item.get("ex_date"))
        prepared.append(item)
    written = upsert_events(prepared)
    changed = False
    for row in prepared:
        key = _key(row)
        old = before.get(key)
        if old is None or any(old.get(field) != row.get(field) for field in (
            "cash_per_unit", "split_ratio", "source",
            "ts_code", "ann_date", "pay_date", "record_date",
        )):
            changed = True
            break
    return written, changed
