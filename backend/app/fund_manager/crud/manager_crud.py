"""基金经理 CRUD：列表/任职史/覆盖率。"""
from __future__ import annotations

from app import db as database


def list_summary(*, keyword="", coverage="all", preset_id=None,
                 skip=0, limit=50, order_field="code", order_dir="asc"):
    """三表 JOIN 分页列表。"""
    return database.list_manager_summary(
        keyword=keyword, coverage=coverage, preset_id=preset_id,
        skip=skip, limit=limit, order_field=order_field, order_dir=order_dir,
    )


def get_tenure(fund_code: str) -> list[dict]:
    """单只基金完整任职史（seq 升序，0=最新）。"""
    return database.select("fund_manager_tenure", [
        ("fund_code", f"eq.{fund_code}"),
        ("order", "seq.asc"),
    ])


def coverage_stats() -> dict:
    """覆盖率统计。"""
    total = database.count("funds")
    covered = database.count("fund_manager_tenure",
                             {"is_current": "eq.1", "seq": "eq.0"})
    return {"total": total, "covered": covered, "uncovered": total - covered}
