"""Resonance 交易对收益计算：ifund 选基 + Resonance 择时的组合回测。"""
from __future__ import annotations

import json
import urllib.request

from app.historical.nav_metrics import get_nav_on_or_before
from app.historical.screen import screen_at_date

RESONANCE_API = "http://localhost:8000/api/resonance/trades?code=510300"

FALLBACK_PAIRS = [
    {"buy": "2025-01-02", "sell": "2025-01-15"},
    {"buy": "2025-04-07", "sell": "2025-07-11"},
    {"buy": "2025-11-21", "sell": "2026-01-28"},
    {"buy": "2026-03-23", "sell": "2026-05-30"},
    {"buy": "2026-07-17", "sell": None},
]


def _fetch_510300_prices(dates: list[str]) -> dict[str, float]:
    """从腾讯行情 API 拉取 510300 日线收盘价。"""
    if not dates:
        return {}
    sorted_dates = sorted(dates)
    start = sorted_dates[0][:8] + "01"
    end = sorted_dates[-1]
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param=sh510300,day,{start},{end},640,qfq")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        klines = data.get("data", {}).get("sh510300", {}).get("qfqday") or \
                 data.get("data", {}).get("sh510300", {}).get("day") or []
        price_by_date = {}
        for k in klines:
            price_by_date[k[0]] = float(k[2])
        result = {}
        for d in dates:
            if d in price_by_date:
                result[d] = price_by_date[d]
            else:
                available = [dt for dt in sorted(price_by_date.keys()) if dt <= d]
                if available:
                    result[d] = price_by_date[available[-1]]
        return result
    except Exception:
        return {}


def fetch_trade_pairs() -> list[dict]:
    """尝试从 Resonance API 拉取交易对，失败则用 fallback。"""
    try:
        req = urllib.request.Request(RESONANCE_API, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        trades = data.get("trades", [])
        pairs = []
        i = 0
        while i < len(trades):
            if trades[i]["action"] == "BUY":
                buy_date = trades[i]["date"]
                buy_price = trades[i]["price"]
                sell_date = None
                sell_price = None
                if i + 1 < len(trades) and trades[i + 1]["action"] == "SELL":
                    sell_date = trades[i + 1]["date"]
                    sell_price = trades[i + 1]["price"]
                    i += 2
                else:
                    i += 1
                pairs.append({
                    "buy": buy_date, "sell": sell_date,
                    "buy_price": buy_price, "sell_price": sell_price,
                })
            else:
                i += 1
        if pairs:
            return pairs
    except Exception:
        pass
    return FALLBACK_PAIRS


def run_backtest(pairs: list[dict] | None = None, top_n: int | None = None,
                 preset_filters: dict | None = None, on_progress=None) -> dict:
    """对每个交易对执行筛选 + 收益计算。

    on_progress: callable(idx, total, msg) 进度回调。
    返回 {"trades": [...], "aggregate": {...}}。
    """
    if pairs is None:
        pairs = fetch_trade_pairs()

    all_dates = []
    for p in pairs:
        all_dates.append(p["buy"])
        if p.get("sell"):
            all_dates.append(p["sell"])
    bench_prices = _fetch_510300_prices(all_dates)
    for p in pairs:
        if not p.get("buy_price") and p["buy"] in bench_prices:
            p["buy_price"] = bench_prices[p["buy"]]
        if p.get("sell") and not p.get("sell_price") and p["sell"] in bench_prices:
            p["sell_price"] = bench_prices[p["sell"]]

    results = []
    for idx, pair in enumerate(pairs):
        buy_date = pair["buy"]
        sell_date = pair.get("sell")
        if on_progress:
            on_progress(idx, len(pairs), f"筛选 {buy_date} ...")

        screen = screen_at_date(buy_date, top_n=top_n, preset_filters=preset_filters)
        if not screen:
            results.append({
                "buy_date": buy_date, "sell_date": sell_date,
                "error": "筛选失败：数据不足",
            })
            continue

        funds = screen["funds"]
        codes = [f["code"] for f in funds]

        buy_navs = get_nav_on_or_before(codes, buy_date)

        if not sell_date:
            fund_results = []
            for f in funds:
                code = f["code"]
                bnav = buy_navs.get(code)
                fund_results.append({
                    "code": code, "name": f["name"], "weight": f["weight"],
                    "cluster": f.get("cluster_label", ""),
                    "return_pct": None,
                })
            results.append({
                "buy_date": buy_date, "sell_date": None,
                "quarter": screen["quarter"],
                "stats": screen["stats"],
                "funds": fund_results,
                "portfolio_return_pct": None,
                "benchmark_return_pct": None,
                "note": "持有中（无卖出日期）",
            })
            continue

        sell_navs = get_nav_on_or_before(codes, sell_date)

        fund_results = []
        portfolio_ret = 0.0
        for f in funds:
            code = f["code"]
            bnav = buy_navs.get(code)
            snav = sell_navs.get(code)
            if bnav and snav:
                ret = snav[1] / bnav[1] - 1.0
            else:
                ret = 0.0
            fund_results.append({
                "code": code, "name": f["name"], "weight": f["weight"],
                "cluster": f.get("cluster_label", ""),
                "return_pct": round(ret * 100, 2),
            })
            portfolio_ret += f["weight"] * ret

        benchmark_ret = None
        bp = pair.get("buy_price")
        sp = pair.get("sell_price")
        if bp and sp and bp > 0:
            benchmark_ret = round((sp / bp - 1.0) * 100, 2)

        fund_results.sort(key=lambda x: x["return_pct"] or 0, reverse=True)

        results.append({
            "buy_date": buy_date,
            "sell_date": sell_date,
            "quarter": screen["quarter"],
            "stats": screen["stats"],
            "funds": fund_results,
            "portfolio_return_pct": round(portfolio_ret * 100, 2),
            "benchmark_return_pct": benchmark_ret,
            "excess_pct": round(portfolio_ret * 100 - benchmark_ret, 2) if benchmark_ret is not None else None,
        })

    completed = [r for r in results if r.get("portfolio_return_pct") is not None]
    bench_completed = [r for r in completed if r.get("benchmark_return_pct") is not None]

    total_ret = 1.0
    bench_total = 1.0
    for r in completed:
        total_ret *= (1 + r["portfolio_return_pct"] / 100)
    for r in bench_completed:
        bench_total *= (1 + r["benchmark_return_pct"] / 100)

    wins = sum(1 for r in bench_completed if (r.get("excess_pct") or 0) > 0)

    aggregate = {
        "total_return_pct": round((total_ret - 1) * 100, 2),
        "benchmark_total_pct": round((bench_total - 1) * 100, 2),
        "excess_total_pct": round((total_ret - bench_total) * 100, 2),
        "win_rate": f"{wins}/{len(bench_completed)}",
        "rounds": len(completed),
    }

    return {"trades": results, "aggregate": aggregate}
