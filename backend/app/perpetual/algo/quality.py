"""质量分计算：滚动夏普/Sortino/风格漂移/z-score 合成。"""
from __future__ import annotations

import numpy as np

from .params import ROLL, W_SHARPE_MED, W_SHARPE_STA, W_SORTINO, W_TENURE, W_DRIFT


def rolling_sharpe_stats(navs: list[float]) -> tuple[float | None, float | None]:
    """滚动 1 年夏普的中位数与标准差。"""
    arr = np.asarray(navs, dtype=np.float64)
    if len(arr) < ROLL + 20:
        return None, None
    rets = np.diff(arr) / arr[:-1]
    sharpes = []
    for i in range(ROLL, len(rets) + 1):
        window = rets[i - ROLL:i]
        std = window.std()
        if std > 0:
            sharpes.append(window.mean() / std * np.sqrt(252))
    if len(sharpes) < 10:
        return None, None
    return float(np.median(sharpes)), float(np.std(sharpes))


def sortino(navs: list[float]) -> float | None:
    """Sortino 比率（阈值=0，只罚下行波动）。"""
    arr = np.asarray(navs, dtype=np.float64)
    if len(arr) < 2:
        return None
    rets = np.diff(arr) / arr[:-1]
    downside = rets[rets < 0]
    if downside.size < 5:
        return None
    dd = np.sqrt(np.mean(downside ** 2))
    if dd == 0:
        return None
    return float(rets.mean() / dd * np.sqrt(252))


def style_drift(quarter_holdings: dict[str, dict[str, float]]) -> float | None:
    """相邻季度前十大持仓的 Bray-Curtis 距离均值。"""
    if not quarter_holdings:
        return None
    quarters = sorted(quarter_holdings.keys())
    if len(quarters) < 2:
        return None
    dists = []
    for i in range(len(quarters) - 1):
        va = quarter_holdings[quarters[i]]
        vb = quarter_holdings[quarters[i + 1]]
        keys = set(va.keys()) | set(vb.keys())
        num = sum(abs(va.get(k, 0.0) - vb.get(k, 0.0)) for k in keys)
        den = sum(va.values()) + sum(vb.values())
        if den > 0:
            dists.append(num / den)
    if not dists:
        return None
    return float(np.mean(dists))


def zscore(vals: list[float | None]) -> list[float]:
    """稳健 z-score：中位数为中心，clip [-2, 2]，None 填 0。"""
    present = [v for v in vals if v is not None]
    if not present:
        return [0.0] * len(vals)
    center = float(np.median(present))
    std = float(np.std(present))
    if std < 1e-12:
        return [0.0] * len(vals)
    out = []
    for v in vals:
        if v is None:
            out.append(0.0)
        else:
            out.append(float(np.clip((v - center) / std, -2, 2)))
    return out


def quality_scores(funds: list[dict]) -> None:
    """就地写入 quality 字段：5 分量稳健 z-score 加权合成。"""
    z_sm = zscore([f.get("sharpe_med") for f in funds])
    z_ss = zscore([-(f["sharpe_std"]) if f.get("sharpe_std") is not None else None for f in funds])
    z_so = zscore([f.get("sortino") for f in funds])
    z_te = zscore([f.get("tenure_days") for f in funds])
    z_dr = zscore([-(f["drift"]) if f.get("drift") is not None else None for f in funds])
    for i, f in enumerate(funds):
        f["quality"] = (W_SHARPE_MED * z_sm[i] + W_SHARPE_STA * z_ss[i]
                        + W_SORTINO * z_so[i] + W_TENURE * z_te[i] + W_DRIFT * z_dr[i])
