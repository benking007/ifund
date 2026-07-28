"""historical 组：历史时点基金筛选 + Resonance 择时回测。"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

DB_PATH = str(Path(__file__).resolve().parents[1] / "data.db")


def _load_preset_filters(preset_id: int) -> dict | None:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT filters_json FROM query_presets WHERE id = ?", (preset_id,)
    ).fetchone()
    conn.close()
    if not row or not row["filters_json"]:
        return None
    return json.loads(row["filters_json"])


def cmd_run(args) -> None:
    from app.historical.backtest import fetch_trade_pairs, run_backtest

    preset_filters = None
    preset_id = getattr(args, "preset", None)
    if preset_id:
        preset_filters = _load_preset_filters(preset_id)
        if not preset_filters:
            print(f"未找到预设 id={preset_id}", file=sys.stderr)
            sys.exit(1)
        print(f"  使用预设 id={preset_id} 过滤条件", file=sys.stderr)

    pairs = fetch_trade_pairs()
    top_n = getattr(args, "top", None)

    def progress(idx, total, msg):
        print(f"  [{idx + 1}/{total}] {msg}", file=sys.stderr)

    result = run_backtest(pairs, top_n=top_n, preset_filters=preset_filters,
                          on_progress=progress)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    for t in result["trades"]:
        buy_d = t["buy_date"]
        sell_d = t.get("sell_date") or "持有中"
        print(f"\n{'=' * 60}")
        print(f"  {buy_d} 买入 → {sell_d} 卖出")
        if t.get("error"):
            print(f"  !! {t['error']}")
            continue
        fb = " (回退)" if t.get("quarter_fallback") else ""
        print(f"  季报: {t.get('quarter')}{fb} | "
              f"NAV池: {t['stats']['total_with_nav']} → "
              f"预筛: {t['stats']['filtered']} → "
              f"有持仓: {t['stats']['with_holdings']} → "
              f"聚类: {t['stats']['clusters']}簇 → "
              f"代表: {t['stats']['selected']}只")
        print(f"  {'代码':<8} {'名称':<20} {'赛道':<14} {'权重':>6} {'收益':>8}")
        print(f"  {'-' * 60}")
        for f in t.get("funds", [])[:15]:
            ret_s = f"{f['return_pct']:+.1f}%" if f["return_pct"] is not None else "  --"
            label = (f.get("cluster") or "")[:12]
            print(f"  {f['code']:<8} {f['name']:<20} {label:<14} "
                  f"{f['weight'] * 100:>5.1f}% {ret_s:>8}")
        if len(t.get("funds", [])) > 15:
            print(f"  ... 共 {len(t['funds'])} 只")
        pr = t.get("portfolio_return_pct")
        br = t.get("benchmark_return_pct")
        ex = t.get("excess_pct")
        if pr is not None:
            line = f"\n  组合收益: {pr:+.2f}%"
            if br is not None:
                line += f" | 基准(510300): {br:+.2f}%"
            if ex is not None:
                line += f" | 超额: {ex:+.2f}%"
            print(line)

    agg = result["aggregate"]
    print(f"\n{'=' * 60}")
    print(f"  汇总（{agg['rounds']} 轮完成）")
    print(f"  组合总收益: {agg['total_return_pct']:+.2f}%")
    print(f"  基准总收益: {agg['benchmark_total_pct']:+.2f}%")
    print(f"  累计超额:   {agg['excess_total_pct']:+.2f}%")
    print(f"  胜率:       {agg['win_rate']}")
