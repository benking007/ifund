"""历史时点筛选编排：预筛 → 加载历史持仓 → 聚类 → 选代表。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from app.cluster.algo import pipeline as cluster_pipeline
from app.historical.nav_metrics import compute_metrics
from app.historical.quarter import latest_disclosed_quarter

DB_PATH = str(Path(__file__).resolve().parents[2] / "data.db")

STOCK_TYPES = ("stock", "股票型", "混合型", "股票指数", "联接")
MIN_NAV_DAYS = 250


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _load_fund_names() -> dict[str, dict]:
    conn = _conn()
    rows = conn.execute(
        "SELECT code, name, fund_type FROM funds"
    ).fetchall()
    conn.close()
    return {r["code"]: {"name": r["name"], "fund_type": r["fund_type"] or ""} for r in rows}


def _load_establish_dates(codes: list[str]) -> dict[str, str]:
    if not codes:
        return {}
    conn = _conn()
    placeholders = ",".join("?" * len(codes))
    rows = conn.execute(
        f"SELECT fund_code, establish_date FROM fund_details "
        f"WHERE fund_code IN ({placeholders})",
        codes,
    ).fetchall()
    conn.close()
    return {r["fund_code"]: r["establish_date"] for r in rows if r["establish_date"]}


def _available_quarters() -> list[str]:
    conn = _conn()
    rows = conn.execute(
        "SELECT DISTINCT quarter FROM fund_holdings ORDER BY quarter"
    ).fetchall()
    conn.close()
    return [r["quarter"] for r in rows]


def _resolve_quarter(buy_date: str) -> tuple[str, bool]:
    """返回 (quarter, is_fallback)。理想季报不存在时回退到最早可用季度。"""
    ideal = latest_disclosed_quarter(buy_date)
    available = _available_quarters()
    if not available:
        return ideal, False
    if ideal in available:
        return ideal, False
    candidates = [q for q in available if q <= ideal]
    if candidates:
        return candidates[-1], True
    return available[0], True


def _load_holdings(quarter: str, codes: set[str]) -> dict[str, list[dict]]:
    conn = _conn()
    rows = conn.execute(
        "SELECT fund_code, asset_code, asset_name, hold_ratio, hold_market_value "
        "FROM fund_holdings WHERE quarter = ? AND holding_type = 'stock'",
        (quarter,),
    ).fetchall()
    conn.close()
    result: dict[str, list[dict]] = {}
    for r in rows:
        code = r["fund_code"]
        if code not in codes:
            continue
        ratio = r["hold_ratio"]
        if not ratio or ratio <= 0:
            continue
        result.setdefault(code, []).append({
            "holding_type": "stock",
            "asset_code": r["asset_code"],
            "asset_name": r["asset_name"] or "",
            "hold_ratio": ratio,
            "hold_market_value": r["hold_market_value"] or 0,
        })
    return result


def _load_detail_filters(codes: list[str]) -> dict[str, dict]:
    if not codes:
        return {}
    conn = _conn()
    placeholders = ",".join("?" * len(codes))
    rows = conn.execute(
        f"SELECT fund_code, fund_type, position_stock, establish_date "
        f"FROM fund_details WHERE fund_code IN ({placeholders})",
        codes,
    ).fetchall()
    conn.close()
    return {r["fund_code"]: {
        "fund_type": r["fund_type"] or "",
        "position_stock": r["position_stock"],
        "establish_date": r["establish_date"],
    } for r in rows}


def _apply_preset_filters(codes: list[str], fund_info: dict[str, dict],
                          metrics: dict[str, dict], filters: dict) -> list[str]:
    """按预设条件过滤（历史安全版本：sharpe_3y 用 NAV 计算值）。"""
    fund_types = filters.get("fund_types") or []
    keyword = filters.get("keyword", "")
    name_excludes = filters.get("name_excludes") or []
    conditions = filters.get("conditions") or []

    cond_map = {}
    for c in conditions:
        cond_map[c["field"]] = (c["op"], c["value"])

    details = _load_detail_filters(codes)

    result = []
    for code in codes:
        info = fund_info.get(code, {})
        name = info.get("name", "")
        detail = details.get(code, {})
        m = metrics.get(code, {})

        if keyword and keyword not in name:
            continue
        if any(ex in name for ex in name_excludes):
            continue
        if fund_types:
            dt = detail.get("fund_type", "")
            if not any(ft in dt for ft in fund_types):
                continue
        if "sharpe_3y" in cond_map:
            op, val = cond_map["sharpe_3y"]
            s3 = m.get("sharpe_3y") or 0
            if op == "gt" and not (s3 > val):
                continue
        if "position_stock" in cond_map:
            op, val = cond_map["position_stock"]
            ps = detail.get("position_stock")
            if ps is None:
                continue
            if op == "gt" and not (ps > val):
                continue
        result.append(code)
    return result


def screen_at_date(buy_date: str, top_n: int | None = None,
                   preset_filters: dict | None = None) -> dict | None:
    """在买入日期执行完整筛选流程。

    preset_filters: 预设过滤条件（同 query_presets.filters_json 格式）。
    返回 {"funds": [...], "quarter": str, "stats": {...}} 或 None。
    """
    fund_info = _load_fund_names()

    metrics = compute_metrics(buy_date)

    filtered_codes = []
    for code, m in metrics.items():
        info = fund_info.get(code)
        if not info:
            continue
        ft = info["fund_type"]
        if not any(t in ft for t in STOCK_TYPES):
            continue
        if m["days"] < MIN_NAV_DAYS:
            continue
        if m["sharpe_1y"] <= 0:
            continue
        filtered_codes.append(code)

    if preset_filters:
        filtered_codes = _apply_preset_filters(
            filtered_codes, fund_info, metrics, preset_filters)

    if len(filtered_codes) < 3:
        return None

    quarter, is_fallback = _resolve_quarter(buy_date)
    holdings = _load_holdings(quarter, set(filtered_codes))

    details = _load_detail_filters(filtered_codes)

    items = []
    for code in filtered_codes:
        h = holdings.get(code)
        if not h:
            continue
        items.append({
            "code": code,
            "name": fund_info[code]["name"],
            "holdings": h,
        })

    if len(items) < 3:
        return None

    cluster_metrics = {}
    for code in filtered_codes:
        m = metrics.get(code, {})
        mdd = m.get("max_drawdown_3y") or 0
        ret = m.get("return_1y") or 0
        rr = round(ret / mdd, 4) if mdd > 0.01 else 0.0
        cluster_metrics[code] = {
            "name": fund_info.get(code, {}).get("name", ""),
            "sharpe_3y": m.get("sharpe_3y"),
            "risk_return_ratio_3y": rr,
            "scale": None,
            "establish_date": details.get(code, {}).get("establish_date"),
        }

    result = cluster_pipeline.run(items, cluster_metrics)
    if not result:
        return None

    clusters = result["clusters"]
    selected = []
    for cl in clusters:
        members = cl.get("funds") or []
        if not members:
            continue
        best = members[0]
        code = best["code"]
        m = metrics.get(code, {})
        selected.append({
            "code": code,
            "name": best.get("name") or fund_info.get(code, {}).get("name", ""),
            "sharpe_3y": m.get("sharpe_3y", 0),
            "return_1y": m.get("return_1y", 0),
            "momentum": m.get("momentum", 0),
            "cluster_label": cl.get("name", ""),
        })

    if top_n and len(selected) > top_n:
        selected = selected[:top_n]

    weight = round(1.0 / len(selected), 4) if selected else 0
    for f in selected:
        f["weight"] = weight

    return {
        "funds": selected,
        "quarter": quarter,
        "quarter_fallback": is_fallback,
        "stats": {
            "total_with_nav": len(metrics),
            "filtered": len(filtered_codes),
            "with_holdings": len(items),
            "clusters": len(clusters),
            "selected": len(selected),
        },
    }
