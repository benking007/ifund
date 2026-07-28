"""批量从 fund_nav 计算时点指标（Sharpe / 最大回撤 / 收益率 / 动量）。

对给定买入日期，一次性加载 3 年窗口内所有基金 NAV，按 fund_code 分组后
用 numpy 向量化计算。避免逐基金查询（10k 基金 × 5 日期 = 50k 次查询太慢）。
"""
from __future__ import annotations

import sqlite3
from itertools import groupby
from operator import itemgetter
from pathlib import Path

import numpy as np

DB_PATH = str(Path(__file__).resolve().parents[2] / "data.db")
TRADING_DAYS_PER_YEAR = 244


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _sharpe_and_drawdown(navs: np.ndarray) -> tuple[float, float]:
    if len(navs) < 20:
        return 0.0, 0.0
    rets = np.diff(navs) / navs[:-1]
    mean = rets.mean()
    std = rets.std(ddof=1)
    annual_return = mean * TRADING_DAYS_PER_YEAR
    annual_vol = std * np.sqrt(TRADING_DAYS_PER_YEAR)
    sharpe = annual_return / annual_vol if annual_vol > 1e-9 else 0.0
    peak = np.maximum.accumulate(navs)
    dd = (navs - peak) / peak
    max_dd = float(-dd.min())
    return round(float(sharpe), 4), round(max_dd, 4)


def _momentum(navs: np.ndarray) -> float:
    n = len(navs)
    if n < 2:
        return 0.0
    def ret(days):
        if n > days:
            return navs[-1] / navs[-1 - days] - 1.0
        return navs[-1] / navs[0] - 1.0
    return 0.5 * ret(21) + 0.3 * ret(63) + 0.2 * ret(126)


def compute_metrics(buy_date: str, lookback_years: int = 3) -> dict[str, dict]:
    """返回 {fund_code: {sharpe_1y, sharpe_3y, max_drawdown_1y, max_drawdown_3y,
    return_1y, momentum, nav_on_date, days}}。"""
    conn = _conn()
    start_date = f"{int(buy_date[:4]) - lookback_years}{buy_date[4:]}"
    rows = conn.execute(
        "SELECT fund_code, trade_date, acc_nav, nav FROM fund_nav "
        "WHERE trade_date >= ? AND trade_date <= ? "
        "ORDER BY fund_code, trade_date",
        (start_date, buy_date),
    ).fetchall()
    conn.close()

    results: dict[str, dict] = {}
    for code, group in groupby(rows, key=itemgetter("fund_code")):
        nav_list = []
        for r in group:
            v = r["acc_nav"] or r["nav"]
            if v and v > 0:
                nav_list.append(float(v))
        n = len(nav_list)
        if n < 60:
            continue
        navs = np.array(nav_list, dtype=np.float64)

        sharpe_3y, mdd_3y = _sharpe_and_drawdown(navs)
        nav_1y = navs[-244:] if n >= 244 else navs
        sharpe_1y, mdd_1y = _sharpe_and_drawdown(nav_1y)

        return_1y = float(navs[-1] / nav_1y[0] - 1.0) if len(nav_1y) > 1 else 0.0
        mom = _momentum(navs)

        results[code] = {
            "sharpe_1y": round(sharpe_1y, 4),
            "sharpe_3y": sharpe_3y,
            "max_drawdown_1y": mdd_1y,
            "max_drawdown_3y": mdd_3y,
            "return_1y": round(return_1y, 4),
            "momentum": round(mom, 4),
            "nav_on_date": float(navs[-1]),
            "days": n,
        }
    return results


def get_nav_on_dates(codes: list[str], dates: list[str]) -> dict[str, dict[str, float]]:
    """返回 {code: {date: acc_nav}}，用于计算持有期收益。"""
    if not codes or not dates:
        return {}
    conn = _conn()
    placeholders_d = ",".join("?" * len(dates))
    rows = conn.execute(
        f"SELECT fund_code, trade_date, acc_nav, nav FROM fund_nav "
        f"WHERE trade_date IN ({placeholders_d}) AND fund_code IN "
        f"({','.join('?' * len(codes))})",
        dates + codes,
    ).fetchall()
    conn.close()
    result: dict[str, dict[str, float]] = {}
    for r in rows:
        code = r["fund_code"]
        v = r["acc_nav"] or r["nav"]
        if v and v > 0:
            result.setdefault(code, {})[r["trade_date"]] = float(v)
    return result


def get_nav_on_or_before(codes: list[str], target_date: str) -> dict[str, tuple[str, float]]:
    """返回 {code: (actual_date, acc_nav)}，取 <= target_date 的最近一条。"""
    if not codes:
        return {}
    conn = _conn()
    result: dict[str, tuple[str, float]] = {}
    placeholders = ",".join("?" * len(codes))
    rows = conn.execute(
        f"SELECT fund_code, trade_date, acc_nav, nav FROM fund_nav "
        f"WHERE fund_code IN ({placeholders}) AND trade_date <= ? "
        f"ORDER BY fund_code, trade_date DESC",
        codes + [target_date],
    ).fetchall()
    conn.close()
    for r in rows:
        code = r["fund_code"]
        if code not in result:
            v = r["acc_nav"] or r["nav"]
            if v and v > 0:
                result[code] = (r["trade_date"], float(v))
    return result
