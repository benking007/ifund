"""基金详情数据访问：过期判定 + 单行 upsert。"""
from __future__ import annotations

import datetime
import json

from app import db as database

EXPIRE_DAYS = 7


def get_detail(fund_code: str) -> dict | None:
    """读取单条详情。"""
    return database.select_one("fund_details", {"fund_code": f"eq.{fund_code}"})


def _fetch_time_expired(row: dict) -> bool:
    """fetch_time 超过 EXPIRE_DAYS 或缺失/无法解析 → 过期。"""
    raw = row.get("fetch_time")
    if not raw:
        return True
    try:
        fetched = datetime.datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return True
    return (datetime.datetime.now() - fetched).days >= EXPIRE_DAYS


def _is_source_unavailable(row: dict) -> bool:
    """占位记录：蛋卷源确认不收录该基金（后端份额/定期开放/部分联接等）。

    标记存于闲置的 detail_json 列：{"source_unavailable": true}。
    此类记录 7 天内视为无需刷新（避免每天空拉），超 7 天由 fetch_time 判据
    自然过期 → 自动重探（蛋卷若补录则恢复）。
    """
    raw = row.get("detail_json")
    if not raw:
        return False
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return False
    return bool(isinstance(payload, dict) and payload.get("source_unavailable"))


def is_expired(fund_code: str, latest_nav_date: str | None) -> bool:
    """详情是否需要刷新。

    任一条件成立即过期：无记录 / fetch_time 超 7 天或无法解析 /
    存储 trade_date 与最新交易日 latest_nav_date 不一致。
    scale 允许为空：蛋卷对部分基金不返回规模字段（basic 接口整体无数据），
    重拉不会改善，2026-09-01 起不再因 scale 缺失触发重拉。
    源不可用占位记录（detail_json.source_unavailable）仅在 fetch_time 超 7 天后
    才重新视为过期（自动重探），期间跳过以避免每天空拉。
    """
    row = get_detail(fund_code)
    if not row:
        return True
    if _is_source_unavailable(row):
        # 占位记录：只按 fetch_time 过期判据，7 天内跳过
        return _fetch_time_expired(row)
    if _fetch_time_expired(row):
        return True
    if latest_nav_date and str(row.get("trade_date") or "") != str(latest_nav_date):
        return True
    return False


def upsert(fund_code: str, columns: dict) -> None:
    """单行 upsert：存在则 update，否则 insert。"""
    if get_detail(fund_code):
        database.update("fund_details", {"fund_code": fund_code}, columns)
    else:
        payload = dict(columns)
        payload["fund_code"] = fund_code
        database.insert("fund_details", payload)
