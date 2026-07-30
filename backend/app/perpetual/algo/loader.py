"""永续组合数据加载层：候选池 + 净值/持仓/任期。"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from app import db as database

from .params import (MAX_SCALE, MIN_SCALE, MIN_TENURE_DAYS, NAME_EXCLUDES,
                     NAV_START, TYPE_PREFIXES)


def load_universe(codes: list[str] | None = None) -> list[dict]:
    """宽口径候选池：规模/类型/名称过滤。"""
    params: list[tuple[str, str]] = [
        ("scale", f"gte.{MIN_SCALE}"),
        ("scale", f"lte.{MAX_SCALE}"),
    ]
    if codes:
        params.append(("fund_code", f"in.({','.join(codes)})"))
    rows = database.select("fund_details", params)
    universe = []
    for r in rows:
        name = r.get("fund_name") or ""
        if any(kw in name for kw in NAME_EXCLUDES):
            continue
        ftype = r.get("fund_type") or ""
        if not any(ftype.startswith(p) for p in TYPE_PREFIXES):
            continue
        universe.append({
            "code": r["fund_code"],
            "name": name,
            "scale": r.get("scale"),
            "pos": r.get("position_stock"),
            "sharpe_3y": r.get("sharpe_3y"),
            "mdd_3y": r.get("max_drawdown_3y"),
            "ytd": r.get("return_ytd"),
            "manager": r.get("fund_manager"),
            "company": r.get("fund_company") or "",
        })
    return universe


def current_tenure_days(code: str, as_of: str | None = None) -> int:
    """现任经理任期天数；as_of 回推近似。"""
    row = database.select_one("fund_manager_tenure", [
        ("fund_code", f"eq.{code}"),
        ("is_current", "eq.1"),
        ("order", "tenure_days.desc"),
    ])
    if not row or row.get("tenure_days") is None:
        return 0
    days = int(row["tenure_days"])
    if as_of:
        t = datetime.strptime(as_of, "%Y-%m-%d").date()
        gap = (date.today() - t).days
        days = max(0, days - gap)
    return days


def load_nav_series(code: str, as_of: str | None = None) -> list[tuple[str, float]]:
    """日频单位净值序列 [(date, nav), ...] 升序。"""
    if as_of:
        t = datetime.strptime(as_of, "%Y-%m-%d").date()
        start = (t - timedelta(days=6 * 365)).isoformat()
    else:
        start = NAV_START
    rows = database.select("fund_nav", [
        ("fund_code", f"eq.{code}"),
        ("trade_date", f"gte.{start}"),
        ("order", "trade_date.asc"),
        ("limit", "5000"),
    ])
    return [(r["trade_date"], r["nav"]) for r in rows if r.get("nav") is not None]


def latest_disclosed_quarter(as_of: str) -> str:
    """T 时刻已披露的最新季度（季末+45天滞后）。"""
    t = datetime.strptime(as_of, "%Y-%m-%d").date()
    candidates = []
    for y in (t.year - 1, t.year):
        for m, d, q in ((3, 31, "Q1"), (6, 30, "Q2"), (9, 30, "Q3"), (12, 31, "Q4")):
            qend = date(y, m, d)
            if qend + timedelta(days=45) <= t:
                candidates.append((qend, f"{y}{q}"))
    if not candidates:
        return f"{t.year - 1}Q4"
    candidates.sort()
    return candidates[-1][1]


def load_quarter_holdings(code: str, as_of: str | None = None) -> dict[str, dict[str, float]]:
    """各季度前十大股票持仓 {quarter: {asset_code: ratio}}。"""
    rows = database.select("fund_holdings", [
        ("fund_code", f"eq.{code}"),
        ("holding_type", "eq.stock"),
    ])
    result: dict[str, dict[str, float]] = {}
    cutoff = latest_disclosed_quarter(as_of) if as_of else None
    for r in rows:
        q = r.get("quarter") or ""
        if cutoff and q > cutoff:
            continue
        if q not in result:
            result[q] = {}
        result[q][r["asset_code"]] = r.get("hold_ratio") or 0.0
    return result
