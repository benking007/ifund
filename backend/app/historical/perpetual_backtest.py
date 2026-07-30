"""永续组合 + Resonance 择时联合回测。

每个买点用永续引擎选基(as_of=买日)，满仓持有到卖点，计算复合收益。
"""
from __future__ import annotations

from app.historical.backtest import fetch_trade_pairs, _fetch_510300_prices
from app.historical.nav_metrics import get_nav_on_or_before
from app.perpetual.api.router import build_result


def run_perpetual_backtest(pairs: list[dict] | None = None,
                           codes: list[str] | None = None,
                           on_progress=None) -> dict:
    """对每个交易对：永续选基 → 持有期收益 → 复合。"""
    if pairs is None:
        pairs = fetch_trade_pairs()

    all_dates = []
    for p in pairs:
        all_dates.append(p["buy"])
        if p.get("sell"):
            all_dates.append(p["sell"])
    bench_prices = _fetch_510300_prices(all_dates)

    results = []
    for idx, pair in enumerate(pairs):
        buy_date = pair["buy"]
        sell_date = pair.get("sell")
        if on_progress:
            on_progress(idx, len(pairs), f"永续选基 {buy_date} ...")

        result = build_result(codes=codes, as_of=buy_date)
        if "error" in result:
            results.append({"buy_date": buy_date, "sell_date": sell_date,
                            "error": result["error"]})
            continue

        holdings = result["holdings"]
        fund_codes = [h["code"] for h in holdings]
        buy_navs = get_nav_on_or_before(fund_codes, buy_date)

        if not sell_date:
            results.append({
                "buy_date": buy_date, "sell_date": None,
                "funds": [{"code": h["code"], "name": h["name"],
                           "weight": h["weight"]} for h in holdings],
                "portfolio_return_pct": None,
                "benchmark_return_pct": None,
                "note": "持有中",
            })
            continue

        sell_navs = get_nav_on_or_before(fund_codes, sell_date)

        fund_results = []
        portfolio_ret = 0.0
        for h in holdings:
            code = h["code"]
            bnav = buy_navs.get(code)
            snav = sell_navs.get(code)
            if bnav and snav:
                ret = snav[1] / bnav[1] - 1.0
            else:
                ret = 0.0
            fund_results.append({
                "code": code, "name": h["name"], "weight": h["weight"],
                "return_pct": round(ret * 100, 2),
            })
            portfolio_ret += h["weight"] * ret

        bp = bench_prices.get(buy_date) or pair.get("buy_price")
        sp = bench_prices.get(sell_date) or pair.get("sell_price")
        benchmark_ret = round((sp / bp - 1.0) * 100, 2) if bp and sp and bp > 0 else None

        fund_results.sort(key=lambda x: x["return_pct"] or 0, reverse=True)
        results.append({
            "buy_date": buy_date,
            "sell_date": sell_date,
            "funds": fund_results,
            "portfolio_return_pct": round(portfolio_ret * 100, 2),
            "benchmark_return_pct": benchmark_ret,
            "excess_pct": round(portfolio_ret * 100 - benchmark_ret, 2)
            if benchmark_ret is not None else None,
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
        "capital": 1000000,
        "final_value": round(1000000 * total_ret, 0),
    }

    return {"trades": results, "aggregate": aggregate}
