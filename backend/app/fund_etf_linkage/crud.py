"""fund_etf_linkage 表访问。"""

from __future__ import annotations

import datetime

from app import db as database

_FIELDS = ("fund_name", "etf_code", "etf_name", "matched_by", "confidence")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def get_linkage(fund_code: str) -> dict | None:
    """按联接基金代码取血缘；无则 None。"""
    return database.select_one("fund_etf_linkage", {"fund_code": f"eq.{fund_code}"})


def delete_linkage(fund_code: str) -> None:
    """删除某联接基金的血缘行。"""
    database.delete("fund_etf_linkage", {"fund_code": fund_code})


def upsert_linkage(data: dict) -> dict:
    """按 fund_code 幂等写入；已存在则更新可变列，保留 id/created_at。"""
    fund_code = str(data.get("fund_code") or "").strip()
    if not fund_code:
        raise ValueError("fund_code required")
    now = _now()
    fields = {key: data.get(key) or "" for key in _FIELDS}
    fields["updated_at"] = now
    existing = get_linkage(fund_code)
    if existing:
        database.update("fund_etf_linkage", {"fund_code": fund_code}, fields)
        return {**existing, **fields}
    return database.insert(
        "fund_etf_linkage",
        {
            "fund_code": fund_code,
            "created_at": now,
            **fields,
        },
    )


def count_by_confidence() -> dict:
    """血缘总量 + 置信度分布。"""
    rows = database.select("fund_etf_linkage", {"select": "confidence"})
    dist = {"high": 0, "medium": 0, "low": 0}
    for row in rows:
        key = row.get("confidence") or "low"
        dist[key] = dist.get(key, 0) + 1
    return {"total": len(rows), "confidence": dist}
