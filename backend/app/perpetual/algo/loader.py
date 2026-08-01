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
    return current_tenure_days_batch([code], as_of).get(code, 0)


def _in_filter(codes: list[str]) -> str:
    """Build the existing database adapter's safe ``in.(...)`` filter."""
    return f"in.({','.join(codes)})"


def _unique_codes(codes: list[str]) -> list[str]:
    """Deduplicate codes while preserving the caller's order."""
    return list(dict.fromkeys(codes))


def current_tenure_days_batch(codes: list[str], as_of: str | None = None) -> dict[str, int]:
    """Batch-load current manager tenure, preserving the single-code semantics."""
    unique_codes = _unique_codes(codes)
    result = {code: 0 for code in unique_codes}
    if not unique_codes:
        return result

    rows = database.select("fund_manager_tenure", [
        ("select", "fund_code,tenure_days"),
        ("fund_code", _in_filter(unique_codes)),
        ("is_current", "eq.1"),
        ("order", "fund_code.asc,tenure_days.desc"),
    ])
    seen: set[str] = set()
    for row in rows:
        code = row.get("fund_code")
        if code not in result or code in seen:
            continue
        seen.add(code)
        if row.get("tenure_days") is not None:
            result[code] = int(row["tenure_days"])

    if as_of:
        t = datetime.strptime(as_of, "%Y-%m-%d").date()
        gap = (date.today() - t).days
        result = {code: max(0, days - gap) for code, days in result.items()}
    return result


def load_nav_series(code: str, as_of: str | None = None) -> list[tuple[str, float]]:
    """日频单位净值序列 [(date, nav), ...] 升序。"""
    return load_nav_series_batch([code], as_of).get(code, [])


def load_nav_series_batch(
    codes: list[str], as_of: str | None = None
) -> dict[str, list[tuple[str, float]]]:
    """Batch-load daily NAV rows and group them in memory by fund code."""
    unique_codes = _unique_codes(codes)
    result: dict[str, list[tuple[str, float]]] = {code: [] for code in unique_codes}
    if not unique_codes:
        return result

    if as_of:
        t = datetime.strptime(as_of, "%Y-%m-%d").date()
        start = (t - timedelta(days=6 * 365)).isoformat()
    else:
        start = NAV_START
    rows = database.select("fund_nav", [
        ("select", "fund_code,trade_date,nav"),
        ("fund_code", _in_filter(unique_codes)),
        ("trade_date", f"gte.{start}"),
        ("order", "fund_code.asc,trade_date.asc"),
    ])
    # The old per-code query applied LIMIT 5000 before dropping NULL NAV rows.
    # Keep that behavior even though the batch query has one global result set.
    row_counts = {code: 0 for code in unique_codes}
    for row in rows:
        code = row.get("fund_code")
        if code not in result or row_counts[code] >= 5000:
            continue
        row_counts[code] += 1
        if row.get("nav") is not None:
            result[code].append((row["trade_date"], row["nav"]))
    return result


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
    return load_quarter_holdings_batch([code], as_of).get(code, {})


def load_quarter_holdings_batch(
    codes: list[str], as_of: str | None = None
) -> dict[str, dict[str, dict[str, float]]]:
    """Batch-load stock holdings and group them by fund and disclosure quarter."""
    unique_codes = _unique_codes(codes)
    result: dict[str, dict[str, dict[str, float]]] = {
        code: {} for code in unique_codes
    }
    if not unique_codes:
        return result

    rows = database.select("fund_holdings", [
        ("select", "fund_code,quarter,asset_code,hold_ratio"),
        ("fund_code", _in_filter(unique_codes)),
        ("holding_type", "eq.stock"),
        ("order", "fund_code.asc,quarter.asc,asset_code.asc"),
    ])
    cutoff = latest_disclosed_quarter(as_of) if as_of else None
    for r in rows:
        code = r.get("fund_code")
        if code not in result:
            continue
        q = r.get("quarter") or ""
        if cutoff and q > cutoff:
            continue
        if q not in result[code]:
            result[code][q] = {}
        result[code][q][r["asset_code"]] = r.get("hold_ratio") or 0.0
    return result
