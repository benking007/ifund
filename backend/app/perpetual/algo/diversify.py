"""分散层：残差相关/风格轴/ENB/贪心选择/风险平价。"""
from __future__ import annotations

import numpy as np


def residual_corr(ret_matrix: np.ndarray) -> tuple[np.ndarray, float]:
    """剥离 PC1（市场 beta）后的残差相关矩阵 + PC1 方差占比。"""
    x = ret_matrix - ret_matrix.mean(axis=1, keepdims=True)
    stds = x.std(axis=1, keepdims=True)
    stds[stds == 0] = 1.0
    z = x / stds
    _, s, vt = np.linalg.svd(z, full_matrices=False)
    pc1 = vt[0]
    pc1_var_ratio = float(s[0] ** 2 / np.sum(s ** 2))
    n = z.shape[0]
    resid = np.empty_like(z)
    pc1_norm = np.dot(pc1, pc1)
    for i in range(n):
        beta_i = np.dot(z[i], pc1) / pc1_norm
        resid[i] = z[i] - beta_i * pc1
    rcorr = np.corrcoef(resid)
    return rcorr, pc1_var_ratio


def style_axes(ret_matrix: np.ndarray, n_axes: int) -> tuple[np.ndarray, np.ndarray]:
    """PC2..PC(1+n_axes) 载荷矩阵（z-score 标准化）+ 各轴方差占比。"""
    x = ret_matrix - ret_matrix.mean(axis=1, keepdims=True)
    stds = x.std(axis=1, keepdims=True)
    stds[stds == 0] = 1.0
    z = x / stds
    u, s, _ = np.linalg.svd(z, full_matrices=False)
    var = s ** 2 / np.sum(s ** 2)
    load = u[:, 1:1 + n_axes].copy()
    col_std = load.std(axis=0)
    col_std[col_std == 0] = 1.0
    load = (load - load.mean(axis=0)) / col_std
    return load, var[1:1 + n_axes]


def enb(corr: np.ndarray) -> float:
    """有效下注数（特征值熵指数）。"""
    ev = np.linalg.eigvalsh(corr)
    ev = ev[ev > 1e-10]
    p = ev / ev.sum()
    return float(np.exp(-np.sum(p * np.log(p))))


def greedy_select(quality: np.ndarray, rcorr: np.ndarray, k: int, lam: float,
                  companies: list[str], max_per_company: int,
                  axes: np.ndarray | None = None, mu: float = 0.0) -> list[int]:
    """贪心选择：综合分 = q01 - lam*残差相关 + mu*风格边际覆盖。"""
    n = len(quality)
    q_min, q_max = quality.min(), quality.max()
    q01 = (quality - q_min) / (q_max - q_min + 1e-9)
    selected: list[int] = [int(np.argmax(q01))]
    comp_count: dict[str, int] = {companies[selected[0]]: 1}

    for _ in range(k - 1):
        best_val = -np.inf
        best_i = -1
        cur_axes = axes[selected] if axes is not None else None
        for i in range(n):
            if i in selected:
                continue
            comp = companies[i]
            if comp_count.get(comp, 0) >= max_per_company:
                continue
            avg_corr = float(np.mean(rcorr[i, selected]))
            val = q01[i] - lam * avg_corr
            if axes is not None and cur_axes is not None and mu > 0:
                lo = cur_axes.min(axis=0)
                hi = cur_axes.max(axis=0)
                cand = axes[i]
                ext = np.clip(cand - hi, 0, None) + np.clip(lo - cand, 0, None)
                val += mu * float(ext.sum())
            if val > best_val:
                best_val = val
                best_i = i
        if best_i < 0:
            break
        selected.append(best_i)
        comp_count[companies[best_i]] = comp_count.get(companies[best_i], 0) + 1
    return selected


def risk_parity(ret_matrix: np.ndarray, wmax: float, iters: int = 500) -> np.ndarray:
    """等风险贡献 (ERC) 迭代配权，单基上限 wmax。"""
    cov = np.cov(ret_matrix)
    n = cov.shape[0]
    w = np.ones(n) / n
    for _ in range(iters):
        mrc = cov @ w
        rc = w * mrc
        target = rc.mean()
        w = w * (target / (rc + 1e-12)) ** 0.5
        w = np.clip(w, 0, wmax)
        w = w / w.sum()
    return w
