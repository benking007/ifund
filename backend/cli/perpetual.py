"""perpetual 组：永续组合生成 / 定期重筛回放。"""
from __future__ import annotations

import json
import sys

from . import helpers, output


def cmd_run(args) -> None:
    """生成永续组合。"""
    from app.perpetual.api.router import build_result

    codes = None
    if args.preset:
        pf = helpers.resolve_preset(args.user, args.preset, None)
        if not pf:
            print("未找到该预设", file=sys.stderr)
            sys.exit(1)
        snap_items = helpers.snapshot_items_raw(args.user, pf["id"])
        codes = [it["code"] for it in snap_items if it.get("code")]
        if not codes:
            print("预设镜像为空，先 preset snapshot", file=sys.stderr)
            sys.exit(1)

    diagnose = args.diagnose.split(",") if args.diagnose else None
    result = build_result(codes, diagnose, include_cloud=False, as_of=args.as_of)
    if "error" in result:
        print(f"错误：{result['error']}", file=sys.stderr)
        sys.exit(1)

    def txt(d):
        s = d["stats"]
        m = d["meta"]
        print(f"候选 {s['universe']} → 过硬门 {s['passed_gate']} → 打分 {s['scored']}"
              f" → 份额去重 -{s['dedup_removed']} → 对齐 {s['aligned_pool']}"
              f"（{s['common_days']} 交易日）")
        print(f"净值截止 {m['nav_as_of']} PC1(市场beta) 方差占比 "
              f"{m['pc1_var_ratio'] * 100:.1f}% 目标持仓 {m['target_holdings']}"
              f" λ={m['lambda_div']} μ={m['mu_style']} wmax={m['wmax']}")
        rows = []
        for h in d["holdings"]:
            axes_str = " ".join(f"{v:+.2f}" for v in (h.get("style_axes") or [])[:2])
            rows.append([
                h["code"], h["name"][:14], (h["company"] or "-")[:8],
                f"{h['weight'] * 100:.1f}%", f"{h['quality']:.3f}",
                f"{h['position_stock']:.0f}%" if h.get("position_stock") else "-",
                f"{h['ytd']:.1f}%" if h.get("ytd") is not None else "-",
                f"{h['tenure_years']:.1f}", f"{h['sharpe_med']:.3f}" if h.get("sharpe_med") else "-",
                axes_str,
            ])
        print(output.table(rows, ["代码", "基金", "公司", "权重", "质量",
                                  "股票仓", "YTD", "任期y", "夏普中", "PC2 PC3"]))
        div = d["diversification"]
        print(f"分散：原始相关均值 {div['orig_corr_mean']:+.3f} → "
              f"残差相关均值 {div['resid_corr_mean']:+.3f} "
              f"有效下注数 ENB {div['enb']:.2f}（目标 {div['enb_target']}）")
        bt = d["backtest"]
        if bt.get("curve"):
            label = "样本外" if m.get("as_of") else "全期"
            c0, c1 = bt["curve"][0], bt["curve"][-1]
            cum = (c1["nav"] / c0["nav"] - 1) * 100
            print(f"回测[{label}]：{c0['date']} ~ {c1['date']} "
                  f"累计 {cum:+.1f}% 年化 {bt['annual_return'] * 100:+.1f}% "
                  f"最大回撤 {bt['max_drawdown'] * 100:.1f}% 夏普 {bt['sharpe']:.2f}")
        if d.get("diagnostics"):
            print("\n落选诊断：")
            for diag in d["diagnostics"]:
                if diag.get("selected"):
                    print(f"  {diag['code']} {diag.get('name', '')} ✓ 已入选")
                elif not diag.get("in_pool"):
                    print(f"  {diag['code']} ✗ 不在候选池")
                else:
                    print(f"  {diag['code']} {diag.get('name', '')} ✗ 未入选 "
                          f"q01={diag.get('q01', 0):.3f} "
                          f"贪心分={diag.get('greedy_score', 0):.3f}")

    if args.json:
        slim = {k: v for k, v in result.items() if k != "cloud"}
        print(json.dumps(slim, ensure_ascii=False, separators=(",", ":")))
    else:
        txt(result)


def cmd_replay(args) -> None:
    """定期重筛回放。"""
    from app.perpetual.algo import replay

    codes = None
    if args.preset:
        pf = helpers.resolve_preset(args.user, args.preset, None)
        if not pf:
            print("未找到该预设", file=sys.stderr)
            sys.exit(1)
        snap_items = helpers.snapshot_items_raw(args.user, pf["id"])
        codes = [it["code"] for it in snap_items if it.get("code")]

    result = replay.run_replay(
        codes=codes, start=args.start,
        step_months=args.step_months, keep_rank=args.keep_rank,
        max_replace=args.max_replace,
    )
    if "error" in result:
        print(f"错误：{result['error']}", file=sys.stderr)
        sys.exit(1)

    def txt(d):
        m = d["meta"]
        print(f"回放 {m['start']} 起 每 {m['step_months']} 月重筛 "
              f"keep_rank={m['keep_rank']} max_replace={m['max_replace']}")
        print(f"锚点：{' → '.join(m['anchors'])}")
        rp, bh = d["replay"], d["buyhold"]
        print(f"\n{'':12s} {'重筛':>10s} {'躺平':>10s}")
        print(f"{'年化收益':12s} {rp['annual_return'] * 100:>+9.1f}% "
              f"{bh['annual_return'] * 100:>+9.1f}%")
        print(f"{'最大回撤':12s} {rp['max_drawdown'] * 100:>9.1f}% "
              f"{bh['max_drawdown'] * 100:>9.1f}%")
        print(f"{'夏普':12s} {rp['sharpe']:>10.2f} {bh['sharpe']:>10.2f}")
        print("\n换手足迹：")
        for t in d["turnover"]:
            swaps_str = ""
            if t["swaps"]:
                parts = [f"{s['out']}→{s['in'] or '?'}" for s in t["swaps"]]
                swaps_str = f" 换 {len(t['swaps'])}（{', '.join(parts)}）"
            note = f" ⚠ {t['note']}" if t.get("note") else ""
            print(f"  {t['anchor']}：留 {t['kept']}{swaps_str}{note}")

    output.emit(result, args.json, txt)
