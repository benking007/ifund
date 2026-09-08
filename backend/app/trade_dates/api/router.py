"""最近交易日蓝图：GET /api/trade-dates/latest。"""
from __future__ import annotations

import datetime

from flask import Blueprint, jsonify

from app import db as database

bp = Blueprint("trade_dates", __name__, url_prefix="/api/trade-dates")


def latest_trade_date(today: str | None = None) -> str | None:
    """≤ today 的最大交易日；空表或查询失败返回 None。"""
    try:
        day = today or datetime.date.today().isoformat()
        row = database.select_one(
            "trade_dates",
            {"trade_date": f"lte.{day}", "order": "trade_date.desc"},
        )
    except Exception:  # pylint: disable=broad-exception-caught
        return None
    if not row:
        return None
    raw = str(row.get("trade_date") or "").strip()
    return raw or None


@bp.get("/latest")
def get_latest():
    """返回 ≤ 今天的最近交易日。空/失败 404。"""
    trade_date = latest_trade_date()
    if not trade_date:
        return jsonify({"detail": "not found"}), 404
    return jsonify({"trade_date": trade_date})
