"""iFund CLI 入口：参数解析 + 分发。

运行：``./venv/bin/python3 -m cli <组> <命令>`` 或 backend 根的 ``ifund_cli.py`` 薄壳。
通用选项 --json（紧凑 JSON）/ --user N（默认 1，主用户）通过 parent parser 注入每个命令。
"""
from __future__ import annotations

import argparse
import datetime
import os
from pathlib import Path

# 加载 .env（CLI 直连 DB 时需要 QODER_PERSONAL_ACCESS_TOKEN 等环境变量）
_env_file = Path(__file__).resolve().parent.parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())

from . import (
    ai_analyze,
    analyze,
    bundle,
    fetch,
    historical,
    holdings,
    nav,
    perpetual,
    preset,
    trade,
)


def _non_negative_int(value: str) -> int:
    """解析 CLI 允许为零的非负整数。"""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是整数") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("必须是非负整数")
    return parsed


def _positive_int(value: str) -> int:
    """解析必须大于零的并发数。"""
    parsed = _non_negative_int(value)
    if parsed == 0:
        raise argparse.ArgumentTypeError("必须大于零")
    return parsed


def _iso_date(raw: str) -> str:
    """解析 YYYY-MM-DD 日期参数。"""
    try:
        return datetime.date.fromisoformat(raw).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期必须是 YYYY-MM-DD") from exc


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="紧凑 JSON 输出")
    common.add_argument("--user", type=int, default=1, help="用户 id（默认 1）")

    parser = argparse.ArgumentParser(prog="ifund", description="iFund CLI（直连 data.db）")
    groups = parser.add_subparsers(dest="group", required=True)

    # preset
    g = groups.add_parser("preset", help="预设与镜像快照").add_subparsers(dest="cmd", required=True)
    g.add_parser("list", parents=[common], help="列出预设").set_defaults(fn=preset.cmd_list)
    p = g.add_parser("show", parents=[common], help="查看预设+镜像基金")
    p.add_argument("--id", type=int)
    p.add_argument("--name")
    p.set_defaults(fn=preset.cmd_show)
    p = g.add_parser("snapshot", parents=[common], help="按预设条件重建镜像")
    p.add_argument("--id", type=int)
    p.add_argument("--name")
    p.add_argument("--limit", type=int, default=500)
    p.set_defaults(fn=preset.cmd_snapshot)
    p = g.add_parser("funds", parents=[common], help="基于预设查询基金（镜像内+基础信息，可过滤）")
    p.add_argument("--id", type=int)
    p.add_argument("--name")
    p.add_argument("--code", help="按基金代码精确过滤（逗号多只）")
    p.add_argument("--keyword", help="按名称/代码模糊过滤")
    p.add_argument("--ai", action="store_true", help="附 AI 定性分析列（评级/实力分/运气/集中/结论）")
    p.set_defaults(fn=preset.cmd_funds)
    p = g.add_parser("ai-set", parents=[common],
                     help="写入/更新某基金的 AI 定性分析（OpenClaw 填充，部分字段 upsert）")
    p.add_argument("--code", required=True, help="基金代码")
    p.add_argument("--data", required=True,
                   help="JSON 对象（字面串 / @文件路径 / - 读 stdin）；字段见 schema fund_ai_analysis")
    p.set_defaults(fn=preset.cmd_ai_set)

    # fetch
    g = groups.add_parser("fetch", help="数据拉取").add_subparsers(dest="cmd", required=True)
    g.add_parser("calendar", parents=[common], help="交易日历").set_defaults(fn=fetch.cmd_calendar)
    p = g.add_parser("industry", parents=[common], help="行业映射")
    p.add_argument("--mode", choices=["sw", "em"], default="sw", help="sw=申万三级 / em=东财兜底")
    p.add_argument("--codes", help="仅重采指定三级行业代码(逗号)")
    p.set_defaults(fn=fetch.cmd_industry)
    for name, fn, helptext in [("detail", fetch.cmd_detail, "基金详情"),
                               ("holdings", fetch.cmd_holdings, "基金持仓"),
                               ("nav", fetch.cmd_nav, "基金净值"),
                               ("manager", fetch.cmd_manager, "基金经理任职史(东财F10)")]:
        p = g.add_parser(name, parents=[common], help=helptext)
        p.add_argument("--codes", help="基金代码(逗号)；省略则按 --types 或全量")
        p.add_argument("--types", help="基金类型(逗号)")
        if name == "holdings":
            p.add_argument(
                "--incremental", action="store_true",
                help="仅拉取缺上一季度持仓的非货币型基金",
            )
        p.set_defaults(fn=fn)

    # nav（净值数据治理：全史回补 / 前复权 / 分红拆分事件）
    g = groups.add_parser("nav", help="净值数据治理")
    g = g.add_subparsers(dest="cmd", required=True)
    p = g.add_parser("backfill", parents=[common], help="东财净值全史回补")
    p.add_argument("--codes", help="基金代码(逗号)")
    p.add_argument("--types", help="基金类型匹配(逗号)")
    p.add_argument("--all", action="store_true", help="处理全部匹配基金；默认仅 pending 队列")
    p.add_argument("--concurrency", type=_positive_int, default=8, help="并发数（默认 8）")
    p.add_argument("--limit", type=_non_negative_int)
    p.set_defaults(fn=nav.cmd_backfill)
    p = g.add_parser("rankbackfill", parents=[common], help="AkShare rank 快照批量补缺")
    p.add_argument("--dry-run", action="store_true", help="只统计缺口，不写 fund_nav")
    p.add_argument("--limit", type=_non_negative_int, help="最多对齐前 N 只 funds 基金")
    p.add_argument(
        "--target-date",
        type=_iso_date,
        help="仅保留快照中该净值日期；接口不支持历史截止日（YYYY-MM-DD）",
    )
    p.set_defaults(fn=nav.cmd_rankbackfill)
    p = g.add_parser("adj", parents=[common], help="Tushare 前复权/事件自算")
    p.add_argument("--codes", help="基金代码(逗号)")
    p.add_argument("--types", help="基金类型匹配(逗号)")
    p.add_argument("--all", action="store_true", help="处理全部匹配基金")
    p.add_argument("--src", choices=["tushare", "calc", "both"], default="both")
    p.add_argument("--only-null", action="store_true", help="calc 只填补 adj_nav 为空的行")
    p.add_argument("--concurrency", type=_positive_int, default=4, help="并发数（默认 4）")
    p.add_argument("--limit", type=_non_negative_int)
    p.set_defaults(fn=nav.cmd_adj)
    p = g.add_parser("events", parents=[common], help="采集 Tushare 分红/拆分事件")
    p.add_argument("--codes", help="基金代码(逗号)")
    p.add_argument("--types", help="基金类型匹配(逗号)")
    p.add_argument("--all", action="store_true", help="处理全部匹配基金")
    p.add_argument("--concurrency", type=_positive_int, default=4, help="并发数（默认 4）")
    p.add_argument("--limit", type=_non_negative_int)
    p.set_defaults(fn=nav.cmd_events)

    # analyze（组合分析）
    g = groups.add_parser("analyze", help="组合分析：预设→仓位建议→穿透/赛道/表现")
    g = g.add_subparsers(dest="cmd", required=True)
    p = g.add_parser("run", parents=[common], help="对预设镜像聚类并算仓位建议")
    p.add_argument("--preset", type=int, required=True, help="预设 id（组合分析必须选一个预设）")
    p.add_argument("--balance", choices=["松", "中", "紧"], help="均衡强度（默认紧 0.14）")
    p.add_argument("--cap", type=float, help="单一行业穿透上限 0.10~0.30（覆盖 balance）")
    p.add_argument("--view", choices=["weights", "industry", "stock", "perf", "all"],
                   default="weights",
                   help="视图：weights=各赛道仓位建议 / industry|stock=底层穿透 / perf=分区间表现 / all")
    p.set_defaults(fn=analyze.cmd_run)
    p = g.add_parser("bundle", parents=[common],
                     help="组装单/多只基金的历史穿透分析数据包（喂 AI 定性分析）")
    p.add_argument("--code", required=True, help="基金代码（逗号可多只）")
    p.set_defaults(fn=bundle.cmd_bundle)

    # ai-analyze（AI 定性分析）
    g = groups.add_parser("ai-analyze", help="AI 定性分析")
    g = g.add_subparsers(dest="cmd", required=True)
    p = g.add_parser("batch", parents=[common], help="批量 AI 定性分析（写入 fund_ai_analysis）")
    p.add_argument("--codes", help="基金代码(逗号分隔)")
    p.add_argument("--preset", type=int, help="预设 id（分析该预设镜像全部基金）")
    p.set_defaults(fn=ai_analyze.cmd_ai_batch)

    # historical（历史时点筛选 + Resonance 择时回测）
    g = groups.add_parser("historical", help="历史时点筛选 + 择时回测")
    g = g.add_subparsers(dest="cmd", required=True)
    p = g.add_parser("run", parents=[common], help="按 Resonance 交易对执行历史筛选回测")
    p.add_argument("--preset", type=int, help="预设 id（用其过滤条件筛选基金）")
    p.add_argument("--top", type=int, help="每轮最多选 N 只（默认全选代表）")
    p.set_defaults(fn=historical.cmd_run)
    p = g.add_parser("perpetual", parents=[common], help="永续组合 + Resonance 择时联合回测")
    p.add_argument("--preset", type=int, help="预设 id（取镜像代码作为候选池）")
    p.set_defaults(fn=historical.cmd_perpetual)

    # perpetual（永续组合）
    g = groups.add_parser("perpetual", help="永续组合：生成/回放")
    g = g.add_subparsers(dest="cmd", required=True)
    p = g.add_parser("run", parents=[common], help="生成永续分散组合")
    p.add_argument("--preset", type=int, help="预设 id（限定候选池）")
    p.add_argument("--diagnose", help="落选诊断基金代码（逗号分隔）")
    p.add_argument("--as-of", dest="as_of", help="决策日 T（YYYY-MM-DD），仅用 ≤T 数据")
    p.set_defaults(fn=perpetual.cmd_run)
    p = g.add_parser("replay", parents=[common], help="定期重筛回放")
    p.add_argument("--start", required=True, help="首个决策日 YYYY-MM-DD")
    p.add_argument("--preset", type=int, help="预设 id（限定候选池）")
    p.add_argument("--step-months", dest="step_months", type=int, default=6, help="重筛间隔月数")
    p.add_argument("--keep-rank", dest="keep_rank", type=int, default=20, help="持仓掉出前 N 名才换")
    p.add_argument("--max-replace", dest="max_replace", type=int, default=3, help="每期最大替换只数")
    p.set_defaults(fn=perpetual.cmd_replay)

    # holdings（实盘：查询 + 交易 + 调仓建议）
    g = groups.add_parser("holdings", help="实盘：持仓查询/交易/调仓建议")
    g = g.add_subparsers(dest="cmd", required=True)
    g.add_parser("list", parents=[common], help="列出实盘（id/名称/关联预设）").set_defaults(fn=holdings.cmd_list)
    p = g.add_parser("show", parents=[common], help="实际持仓（按赛道簇分组，可加 --penetration 附底层穿透）")
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--penetration", action="store_true", help="附带底层穿透（行业/个股）")
    p.add_argument("--by", choices=["industry", "stock", "both"], default="both",
                   help="穿透粒度（仅 --penetration 时生效）")
    p.set_defaults(fn=holdings.cmd_show)
    p = g.add_parser("penetration", parents=[common], help="底层持仓穿透")
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--by", choices=["industry", "stock", "both"], default="both")
    p.set_defaults(fn=holdings.cmd_penetration)
    p = g.add_parser("perf", parents=[common], help="组合分区间表现")
    p.add_argument("--pid", type=int, required=True)
    p.set_defaults(fn=holdings.cmd_perf)
    p = g.add_parser("rebalance", parents=[common], help="调仓建议（生成操作指南）")
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--preset", type=int, help="临时覆盖关联预设")
    p.add_argument("--cap", type=float, help="单一行业穿透上限 0.10~0.30（默认取实盘 cap）")
    p.add_argument("--band", type=float, help="缓冲带（盘子占比，0~0.10，默认 0.03）")
    p.add_argument("--sell-outside", dest="sell_outside", action="store_true",
                   help="允许卖出赛道外基金去补缺口（默认保留不动）")
    p.add_argument("--trim-overflow", dest="trim_overflow", action=argparse.BooleanOptionalAction,
                   default=True, help="赛道内超配是否可减（默认可减；--no-trim-overflow 则不减只加）")
    p.set_defaults(fn=holdings.cmd_rebalance)

    # 交易写操作（buy/sell/transfer + 交易记录 list/del）
    for name, fn, helptext in [("buy", trade.cmd_buy, "买入一笔"), ("sell", trade.cmd_sell, "卖出一笔")]:
        p = g.add_parser(name, parents=[common], help=helptext)
        p.add_argument("--pid", type=int, required=True)
        p.add_argument("--fund", required=True, help="基金代码(6位数字)或名称")
        p.add_argument("--amount", type=float, required=True, help="金额(元)")
        p.add_argument("--date", help="交易日 YYYY-MM-DD（默认最近交易日）")
        p.set_defaults(fn=fn)
    p = g.add_parser("transfer", parents=[common], help="转仓（卖A买B）")
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--from", dest="from_", required=True, help="转出基金 代码/名称")
    p.add_argument("--to", dest="to", required=True, help="转入基金 代码/名称")
    p.add_argument("--amount", type=float, required=True, help="金额(元)")
    p.add_argument("--date", help="交易日 YYYY-MM-DD（默认最近交易日）")
    p.set_defaults(fn=trade.cmd_transfer)
    p = g.add_parser("txns", parents=[common], help="交易记录列表")
    p.add_argument("--pid", type=int, required=True)
    p.set_defaults(fn=trade.cmd_txns)
    p = g.add_parser("txn-del", parents=[common], help="删除一条交易记录（转仓连带删配对）")
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--id", type=int, required=True, help="交易记录 id")
    p.set_defaults(fn=trade.cmd_txn_del)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
