"""永续组合五阶段编排引擎 + 回测/分散/诊断。"""
from __future__ import annotations

import re
from datetime import datetime

import numpy as np

from . import diversify, quality as quality_mod
from .params import (ALIGN_START, LAMBDA_DIV, MAX_PER_COMPANY, MIN_COMMON_DAYS,
                     MIN_NAV_DAYS, MIN_TENURE_DAYS, MU_STYLE, N_STYLE_AXES,
                     TARGET_HOLDINGS, WMAX)


def _share_key(name: str) -> str:
    return re.sub(r"[A-Za-z]+$", "", name).strip()


def _apply_hard_gate(funds: list[dict], nav_by_code: dict, tenure_by_code: dict,
                     as_of: str | None) -> list[dict]:
    passed = []
    for f in funds:
        code = f["code"]
        tenure = tenure_by_code.get(code, 0)
        if tenure < MIN_TENURE_DAYS:
            continue
        series = nav_by_code.get(code) or []
        check_series = [(d, v) for d, v in series if d <= as_of] if as_of else series
        if len(check_series) < MIN_NAV_DAYS:
            continue
        f["tenure_days"] = tenure
        f["dates"] = [d for d, _ in series]
        f["navs"] = [v for _, v in series]
        if as_of:
            yr = as_of[:4]
            base_nav = None
            for d, v in check_series:
                if d >= f"{yr}-01-01":
                    base_nav = v
                    break
            if base_nav and base_nav > 0:
                f["ytd"] = (check_series[-1][1] / base_nav - 1) * 100
        passed.append(f)
    return passed


def _score_quality(funds: list[dict], holdings_by_code: dict,
                   as_of: str | None = None) -> list[dict]:
    for f in funds:
        code = f["code"]
        navs = f["navs"]
        if as_of:
            navs = [v for d, v in zip(f["dates"], navs) if d <= as_of]
        sm, ss = quality_mod.rolling_sharpe_stats(navs)
        f["sharpe_med"] = sm
        f["sharpe_std"] = ss
        f["sortino"] = quality_mod.sortino(navs)
        f["drift"] = quality_mod.style_drift(holdings_by_code.get(code) or {})
    scored = [f for f in funds if f["sharpe_med"] is not None]
    if len(scored) < TARGET_HOLDINGS:
        return []
    quality_mod.quality_scores(scored)
    scored.sort(key=lambda f: f["quality"], reverse=True)
    return scored


def _dedup_shares(funds: list[dict]) -> tuple[list[dict], int]:
    best: dict[str, dict] = {}
    for f in funds:
        key = _share_key(f["name"])
        if key not in best or f["quality"] > best[key]["quality"]:
            best[key] = f
    pool = list(best.values())
    pool.sort(key=lambda f: f["quality"], reverse=True)
    return pool, len(funds) - len(pool)


def _align_window(pool: list[dict], as_of: str | None) -> tuple[list[dict], list[str]]:
    start = ALIGN_START
    all_dates: set[str] = set()
    for f in pool:
        if as_of:
            f["_win_dates"] = {d for d in f["dates"] if start <= d <= as_of}
        else:
            f["_win_dates"] = {d for d in f["dates"] if d >= start}
        all_dates |= f["_win_dates"]
    if not all_dates:
        return [], []
    total = len(all_dates)
    aligned = [f for f in pool if len(f["_win_dates"]) >= total * 0.95]
    if not aligned:
        return [], []
    common = sorted(set.intersection(*(f["_win_dates"] for f in aligned)))
    if len(common) < MIN_COMMON_DAYS:
        return [], common
    for f in aligned:
        navmap = dict(zip(f["dates"], f["navs"]))
        f["_win_navs"] = [navmap[d] for d in common]
    return aligned, common


def _backtest_section(selected_funds: list[dict], weights: np.ndarray) -> dict:
    """用选出的基金完整净值（ALIGN_START→最新）合成回测曲线。"""
    all_dates_sets = []
    for f in selected_funds:
        all_dates_sets.append({d for d in f["dates"] if d >= ALIGN_START})
    if not all_dates_sets:
        return {"curve": [], "max_drawdown": 0, "annual_return": 0,
                "annual_vol": 0, "sharpe": 0}
    common = sorted(set.intersection(*all_dates_sets))
    if len(common) < 2:
        return {"curve": [], "max_drawdown": 0, "annual_return": 0,
                "annual_vol": 0, "sharpe": 0}
    nav_arrays = []
    for f in selected_funds:
        navmap = dict(zip(f["dates"], f["navs"]))
        nav_arrays.append([navmap[d] for d in common])
    mat = np.array(nav_arrays, dtype=np.float64)
    w = weights / weights.sum()
    base = mat[:, 0:1]
    base[base == 0] = 1.0
    normed = mat / base
    port = (normed * w[:, None]).sum(axis=0)
    peak = np.maximum.accumulate(port)
    dd = (port - peak) / peak
    first_d = datetime.strptime(common[0], "%Y-%m-%d").date()
    last_d = datetime.strptime(common[-1], "%Y-%m-%d").date()
    span = max((last_d - first_d).days, 1)
    ann_ret = float(port[-1] ** (365.25 / span) - 1)
    daily_rets = np.diff(port) / port[:-1]
    periods_per_year = len(daily_rets) * 365.25 / span
    ann_vol = float(daily_rets.std() * np.sqrt(periods_per_year))
    sharpe = ann_ret / ann_vol if ann_vol > 0 else 0.0
    curve = [{"date": common[i], "nav": round(float(port[i]), 6),
              "drawdown": round(float(dd[i]), 6)} for i in range(len(common))]
    return {"curve": curve, "max_drawdown": round(float(dd.min()), 6),
            "annual_return": round(ann_ret, 6), "annual_vol": round(ann_vol, 6),
            "sharpe": round(sharpe, 4)}


def _diversification_section(rets: np.ndarray, sel: list[int],
                             rcorr: np.ndarray) -> dict:
    sub_orig = np.corrcoef(rets[sel])
    sub_rcorr = rcorr[sel][:, sel]
    n = len(sel)
    off = ~np.eye(n, dtype=bool)
    return {
        "orig_corr_mean": round(float(sub_orig[off].mean()), 4),
        "resid_corr_mean": round(float(sub_rcorr[off].mean()), 4),
        "enb": round(diversify.enb(sub_orig), 2),
        "enb_target": n,
    }


def _diagnose(pool: list[dict], sel: list[int], q01: np.ndarray,
              rcorr: np.ndarray, axes: np.ndarray | None,
              diagnose_codes: list[str]) -> list[dict]:
    code_idx = {pool[i]["code"]: i for i in range(len(pool))}
    results = []
    for code in diagnose_codes:
        idx = code_idx.get(code)
        if idx is None:
            results.append({"code": code, "in_pool": False, "selected": False})
            continue
        is_sel = idx in sel
        entry: dict = {"code": code, "name": pool[idx]["name"], "in_pool": True,
                       "selected": is_sel, "q01": round(float(q01[idx]), 4)}
        if axes is not None:
            entry["style_axes"] = [round(float(v), 3) for v in axes[idx]]
        if not is_sel and sel:
            avg_corr = float(np.mean(rcorr[idx, sel]))
            entry["avg_corr_to_selected"] = round(avg_corr, 4)
            entry["greedy_score"] = round(float(q01[idx] - LAMBDA_DIV * avg_corr), 4)
            weakest = min(sel, key=lambda s: q01[s])
            entry["weakest_selected"] = {"code": pool[weakest]["code"],
                                         "name": pool[weakest]["name"],
                                         "q01": round(float(q01[weakest]), 4)}
        results.append(entry)
    return results


def _cloud_rows(pool: list[dict], sel: list[int], q01: np.ndarray,
                axes: np.ndarray | None) -> list[dict]:
    if axes is None:
        return []
    sel_set = set(sel)
    rows = []
    for i, f in enumerate(pool):
        rows.append({"code": f["code"], "name": f["name"],
                     "pc2": round(float(axes[i, 0]), 3),
                     "pc3": round(float(axes[i, 1]), 3) if axes.shape[1] > 1 else 0.0,
                     "q01": round(float(q01[i]), 4),
                     "selected": i in sel_set})
    return rows


def run(funds: list[dict], nav_by_code: dict, tenure_by_code: dict,
        holdings_by_code: dict, diagnose_codes: list[str] | None = None,
        include_cloud: bool = False, as_of: str | None = None,
        shortlist_extra: int = 0) -> dict:
    """五阶段流水线主入口。"""
    universe_n = len(funds)
    passed = _apply_hard_gate(funds, nav_by_code, tenure_by_code, as_of)
    passed_n = len(passed)
    scored = _score_quality(passed, holdings_by_code, as_of)
    if not scored:
        return {"error": f"过门后不足 {TARGET_HOLDINGS} 只可打分基金（过门 {passed_n}）"}
    scored_n = len(scored)
    pool, dedup_removed = _dedup_shares(scored)
    aligned, common = _align_window(pool, as_of)
    if not aligned:
        return {"error": f"对齐后共同交易日不足 {MIN_COMMON_DAYS}（{len(common)} 天）"}
    navmap_list = []
    codes_order = []
    for f in aligned:
        navmap_list.append(f["_win_navs"])
        codes_order.append(f["code"])
    mat = np.array(navmap_list, dtype=np.float64)
    rets = np.diff(mat, axis=1) / mat[:, :-1]
    rcorr, pc1_var = diversify.residual_corr(rets)
    axes, axes_var = diversify.style_axes(rets, N_STYLE_AXES)
    qual = np.array([f["quality"] for f in aligned])
    companies = [f["company"] for f in aligned]
    k_total = TARGET_HOLDINGS + shortlist_extra
    sel_ext = diversify.greedy_select(qual, rcorr, k_total, LAMBDA_DIV,
                                      companies, MAX_PER_COMPANY,
                                      axes=axes, mu=MU_STYLE)
    sel = sel_ext[:TARGET_HOLDINGS]
    weights = diversify.risk_parity(rets[sel], WMAX)
    q_min, q_max = qual.min(), qual.max()
    q01 = (qual - q_min) / (q_max - q_min + 1e-9)
    nav_as_of = common[-1] if common else None
    holdings_out = []
    for rank, i in enumerate(sel):
        f = aligned[i]
        holdings_out.append({
            "code": f["code"], "name": f["name"], "company": f["company"],
            "weight": round(float(weights[rank]), 4),
            "quality": round(f["quality"], 4),
            "position_stock": f.get("pos"),
            "ytd": f.get("ytd"),
            "tenure_years": round(f["tenure_days"] / 365.25, 1),
            "sharpe_med": round(f["sharpe_med"], 4) if f["sharpe_med"] else None,
            "style_axes": [round(float(v), 3) for v in axes[i]],
        })
    result: dict = {
        "stats": {"universe": universe_n, "passed_gate": passed_n,
                  "scored": scored_n, "dedup_removed": dedup_removed,
                  "aligned_pool": len(aligned), "common_days": len(common)},
        "meta": {"target_holdings": TARGET_HOLDINGS, "n_selected": len(sel),
                 "align_start": as_of or ALIGN_START, "nav_as_of": nav_as_of,
                 "pc1_var_ratio": round(pc1_var, 4),
                 "style_axis_var": [round(float(v), 4) for v in axes_var],
                 "lambda_div": LAMBDA_DIV, "mu_style": MU_STYLE,
                 "wmax": WMAX, "as_of": as_of},
        "holdings": holdings_out,
        "diversification": _diversification_section(rets, sel, rcorr),
        "backtest": _backtest_section([aligned[i] for i in sel], weights),
    }
    if diagnose_codes:
        result["diagnostics"] = _diagnose(aligned, sel, q01, rcorr, axes,
                                          diagnose_codes)
    if include_cloud:
        result["cloud"] = _cloud_rows(aligned, sel, q01, axes)
    if shortlist_extra > 0 and len(sel_ext) > TARGET_HOLDINGS:
        extra = sel_ext[TARGET_HOLDINGS:]
        result["_shortlist"] = [
            {"code": aligned[i]["code"], "name": aligned[i]["name"],
             "q01": round(float(q01[i]), 4)} for i in extra
        ]
    return result
