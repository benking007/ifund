"""血缘统计蓝图：/api/fund-linkage。"""

from __future__ import annotations

from flask import Blueprint, jsonify

from app.fund_etf_linkage import crud

bp = Blueprint("fund_linkage", __name__, url_prefix="/api/fund-linkage")


@bp.get("/count")
def linkage_count():
    """血缘总量与置信度分布，供回填后自检。"""
    return jsonify(crud.count_by_confidence())
