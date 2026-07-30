"""定期重筛回放：逐锚点 point-in-time 重选 + 滞后替换 + 链乘拼接。"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

import numpy as np

from . import diversify, loader, pipeline
from .params import TARGET_HOLDINGS, WMAX


def _share_key(name: str) -> str:
    return re.sub(r"[A-Za-z]+$", "", name).strip()


def _anchors(start: str, step_months: int) -> list[str]:
    t = datetime.strptime(start, "%Y-%m-%d").date()
    today = date.today()
    out = []
    while t <= today:
        out.append(t.isoformat())
        m = t.month - 1 + step_months
        y = t.year + m // 12
        m = m % 12 + 1
        d = min(t.day, 28)
        t = date(y, m, d)
    return out


def _select_at(anchor: str, universe: list[dict], nav_by_code: dict,
               tenure_now: dict, holdings_now: dict,
               keep_rank: int) -> dict:
    gap = (date.today() - datetime.strptime(anchor, "%Y-%m-%d").date()).days
    tenure_at = {c: max(0, d - gap) for c, d in tenure_now.items()}
    holdings_at = {}
    cutoff = loader.latest_disclosed_quarter(anchor)
    for code, qh in holdings_now.items():
        holdings_at[code] = {q: h for q, h in qh.items() if q <= cutoff}
    shortlist_extra = max(0, keep_rank - TARGET_HOLDINGS)
    return pipeline.run(universe, nav_by_code, tenure_at, holdings_at,
                        as_of=anchor, shortlist_extra=shortlist_extra)


def _rebalance(prev_codes: list[str], fresh: list[dict], shortlist: list[dict],
               keep_rank: int, max_replace: int) -> tuple[list[str], list[dict]]:
    fresh_codes = [h["code"] for h in fresh]
    sl_codes = [s["code"] for s in shortlist]
    safe_set = set(fresh_codes + sl_codes)
    kept = list(prev_codes)
    dropped = []
    for code in prev_codes:
        if code in safe_set:
            continue
        if any(_share_key(h["name"]) == _share_key(code) for h in fresh):
            continue
        dropped.append(code)
    q01_map = {h["code"]: h.get("q01", 0) for h in fresh}
    q01_map.update({s["code"]: s.get("q01", 0) for s in shortlist})
    dropped.sort(key=lambda c: q01_map.get(c, 0))
    swaps = []
    n_replace = min(len(dropped), max_replace)
    candidates = [c for c in fresh_codes + sl_codes if c not in kept]
    for i in range(n_replace):
        out_code = dropped[i]
        in_code = candidates[i] if i < len(candidates) else None
        if in_code:
            kept[kept.index(out_code)] = in_code
            swaps.append({"out": out_code, "in": in_code})
        else:
            kept.remove(out_code)
            swaps.append({"out": out_code, "in": None})
    return kept, swaps


def _weights_at(codes: list[str], nav_by_code: dict, anchor: str) -> np.ndarray:
    t = datetime.strptime(anchor, "%Y-%m-%d").date()
    start = (t - timedelta(days=3 * 365)).isoformat()
    series = {}
    for code in codes:
        full = nav_by_code.get(code) or []
        win = [(d, v) for d, v in full if start <= d <= anchor]
        if win:
            series[code] = win
    valid = [c for c in codes if c in series and len(series[c]) > 100]
    if len(valid) < 2:
        return np.ones(len(codes)) / len(codes)
    all_dates = sorted(set.intersection(*(
        {d for d, _ in series[c]} for c in valid)))
    if len(all_dates) < 100:
        return np.ones(len(codes)) / len(codes)
    navmap_list = []
    for c in valid:
        nm = dict(series[c])
        navmap_list.append([nm[d] for d in all_dates])
    mat = np.array(navmap_list, dtype=np.float64)
    rets = np.diff(mat, axis=1) / mat[:, :-1]
    w = diversify.risk_parity(rets, WMAX)
    full_w = np.zeros(len(codes))
    for i, c in enumerate(codes):
        if c in valid:
            full_w[i] = w[valid.index(c)]
    s = full_w.sum()
    return full_w / s if s > 0 else np.ones(len(codes)) / len(codes)


def _segment(codes: list[str], weights: np.ndarray, nav_by_code: dict,
             start: str, end: str) -> list[tuple[str, float]]:
    series = {}
    for code in codes:
        full = nav_by_code.get(code) or []
        win = [(d, v) for d, v in full if start <= d < end]
        if win:
            series[code] = win
    valid = [c for c in codes if c in series]
    if not valid:
        return []
    common = sorted(set.intersection(*(
        {d for d, _ in series[c]} for c in valid)))
    if len(common) < 2:
        return []
    w = np.array([weights[codes.index(c)] for c in valid])
    w = w / w.sum()
    navs = []
    for c in valid:
        nm = dict(series[c])
        base = nm[common[0]]
        navs.append([nm[d] / base if base else 1.0 for d in common])
    port = (np.array(navs) * w[:, None]).sum(axis=0)
    return [(common[i], float(port[i])) for i in range(len(common))]


def run_replay(codes: list[str] | None = None, start: str = "2024-01-01",
               step_months: int = 6, keep_rank: int = 20,
               max_replace: int = 3) -> dict:
    """定期重筛回放主入口。"""
    anchors = _anchors(start, step_months)
    if len(anchors) < 2:
        return {"error": "锚点不足（start 太近或 step_months 太大）"}
    universe = loader.load_universe(codes)
    nav_by_code = {f["code"]: loader.load_nav_series(f["code"], anchors[0])
                   for f in universe}
    tenure_now = {f["code"]: loader.current_tenure_days(f["code"])
                  for f in universe}
    holdings_now = {f["code"]: loader.load_quarter_holdings(f["code"])
                    for f in universe}
    stitched: list[tuple[str, float]] = []
    bh_curve: list[tuple[str, float]] = []
    turnover = []
    prev_codes: list[str] = []
    last_nav = 1.0
    first_weights: np.ndarray | None = None
    first_codes: list[str] = []

    for k, anchor in enumerate(anchors):
        end = anchors[k + 1] if k + 1 < len(anchors) else \
            (date.today() + timedelta(days=1)).isoformat()
        res = _select_at(anchor, universe, nav_by_code, tenure_now,
                         holdings_now, keep_rank)
        if "error" in res:
            turnover.append({"anchor": anchor, "kept": len(prev_codes),
                             "swaps": [], "note": res["error"]})
            continue
        fresh = res.get("holdings", [])
        shortlist = res.get("_shortlist", [])
        fresh_codes = [h["code"] for h in fresh]
        if k == 0:
            cur_codes = fresh_codes
            swaps = []
        else:
            cur_codes, swaps = _rebalance(prev_codes, fresh, shortlist,
                                          keep_rank, max_replace)
        weights = _weights_at(cur_codes, nav_by_code, anchor)
        seg = _segment(cur_codes, weights, nav_by_code, anchor, end)
        if seg:
            stitched.extend((d, v * last_nav) for d, v in seg)
            last_nav = stitched[-1][1]
        if k == 0:
            first_codes = cur_codes
            first_weights = weights
            bh_seg = _segment(cur_codes, weights, nav_by_code, anchor,
                              (date.today() + timedelta(days=1)).isoformat())
            bh_curve = bh_seg
        turnover.append({
            "anchor": anchor,
            "kept": len(cur_codes) - len(swaps),
            "swaps": swaps,
            "note": None,
        })
        prev_codes = cur_codes

    def _curve_stats(curve: list[tuple[str, float]]) -> dict:
        if len(curve) < 2:
            return {"curve": [], "max_drawdown": 0, "annual_return": 0,
                    "annual_vol": 0, "sharpe": 0}
        navs = np.array([v for _, v in curve])
        dates = [d for d, _ in curve]
        peak = np.maximum.accumulate(navs)
        dd = (navs - peak) / peak
        first_d = datetime.strptime(dates[0], "%Y-%m-%d").date()
        last_d = datetime.strptime(dates[-1], "%Y-%m-%d").date()
        span = max((last_d - first_d).days, 1)
        ann_ret = float(navs[-1] ** (365.25 / span) - 1)
        daily_rets = np.diff(navs) / navs[:-1]
        ppy = len(daily_rets) * 365.25 / span
        ann_vol = float(daily_rets.std() * np.sqrt(ppy))
        sharpe = ann_ret / ann_vol if ann_vol > 0 else 0.0
        return {
            "curve": [{"date": dates[i], "nav": round(float(navs[i]), 6),
                       "drawdown": round(float(dd[i]), 6)}
                      for i in range(len(dates))],
            "max_drawdown": round(float(dd.min()), 6),
            "annual_return": round(ann_ret, 6),
            "annual_vol": round(ann_vol, 6),
            "sharpe": round(sharpe, 4),
        }

    return {
        "meta": {"start": start, "step_months": step_months,
                 "keep_rank": keep_rank, "max_replace": max_replace,
                 "anchors": anchors},
        "replay": _curve_stats(stitched),
        "buyhold": _curve_stats(bh_curve),
        "turnover": turnover,
    }
