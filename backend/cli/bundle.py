"""analyze bundle：为单只基金组装「历史穿透分析数据包」（喂给 AI 定性分析的确定性输入）。

把散落在 fund_details / fund_manager_tenure / fund_holdings / fund_cum_return 的数据，
预先算成 AI 能直接推理的结构化事实——尤其是两件「基于历史而非当下」的判断：
  1) 经理归因：当前团队/主理人的连续任期是否覆盖 3y/5y 业绩窗口 → 业绩算不算他的；
  2) 持仓漂移：跨 5 个季度的前十大集中度趋势 + 相邻季重合度 → 是否正在变「单押」。
脚本只产出事实与派生指标，价值判断（打分/结论）交给 AI（见 Prompt-基金AI历史穿透分析.md）。
"""
from __future__ import annotations

import datetime
import json
import sys

from app import db as database

_TODAY = datetime.date.today()


# ---------- 小工具 ----------
def _f(v):
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return None


def _clip(text, n: int):
    """招募书类长文截断（去换行/空白），只留开头供 AI 判断风格取向。"""
    if not text:
        return None
    s = " ".join(str(text).split())
    return s if len(s) <= n else s[:n] + "…"


def _years_ago(years: float) -> str:
    return (_TODAY - datetime.timedelta(days=int(years * 365))).isoformat()


def _name_set(managers: str) -> frozenset[str]:
    return frozenset((managers or "").split())


# ---------- 经理归因 ----------
def _manager_block(code: str) -> dict:
    segs = database.select("fund_manager_tenure",
                           {"fund_code": f"eq.{code}", "order": "seq.asc"})
    if not segs:
        return {"available": False}
    cur = segs[0]
    cur_set = _name_set(cur["managers"])
    cur_lead = (cur["managers"] or "").split()[:1]
    cur_lead = cur_lead[0] if cur_lead else None

    # 当前「完全相同团队」的连续起始日：从 seq0 往早走，团队字符串一致则延伸
    exact_start = cur["start_date"]
    for seg in segs[1:]:
        if (seg["managers"] or "").strip() == (cur["managers"] or "").strip():
            exact_start = seg["start_date"]
        else:
            break
    # 主理人（首位经理）连续在任起始日：只要该名字仍在该段团队里就延伸（容忍共管更替）
    lead_start = cur["start_date"]
    if cur_lead:
        for seg in segs:
            if cur_lead in _name_set(seg["managers"]):
                lead_start = seg["start_date"]
            else:
                break

    def _covers(start: str | None, yrs: float) -> bool | None:
        if not start:
            return None
        return start <= _years_ago(yrs)

    # 不同团队组合数（合并相邻相同）= 换团队次数+1
    distinct_sets = 0
    prev = None
    for seg in segs:
        key = (seg["managers"] or "").strip()
        if key != prev:
            distinct_sets += 1
            prev = key

    return {
        "available": True,
        "current": cur["managers"],
        "current_lead": cur_lead,
        "is_comanaged": len(cur_set) > 1,
        "exact_team_since": exact_start,
        "lead_since": lead_start,
        "exact_team_years": _f((_TODAY - datetime.date.fromisoformat(exact_start)).days / 365) if exact_start else None,
        "lead_years": _f((_TODAY - datetime.date.fromisoformat(lead_start)).days / 365) if lead_start else None,
        "segment_count": len(segs),
        "distinct_team_count": distinct_sets,   # >1 说明历史上换过团队
        "exact_team_covers": {"1y": _covers(exact_start, 1), "3y": _covers(exact_start, 3), "5y": _covers(exact_start, 5)},
        "lead_covers": {"1y": _covers(lead_start, 1), "3y": _covers(lead_start, 3), "5y": _covers(lead_start, 5)},
        # 最近 6 段原始任职记录（够 AI 看清最近的更替脉络；更早的省略）
        "recent_segments": [
            {"start": s["start_date"], "end": s["end_date"] or "至今",
             "managers": s["managers"], "tenure_days": s["tenure_days"],
             "tenure_return_pct": s["tenure_return"]}
            for s in segs[:6]
        ],
    }


# ---------- 持仓漂移 ----------
# 申万一级 → 大主题（粗分组，用于「是不是单押某条赛道」的判断）：
# 相邻一级常同涨同跌、属同一投资主题（如电子/通信/计算机/传媒都是泛科技），
# 只看单一申万一级会低估集中度，故再聚一层主题。仅作线索，原始 sw_l1_mix 一并给出。
_THEME = {
    "电子": "泛科技TMT", "通信": "泛科技TMT", "计算机": "泛科技TMT", "传媒": "泛科技TMT",
    "电力设备": "新能源链", "有色金属": "周期资源", "基础化工": "周期资源", "钢铁": "周期资源",
    "煤炭": "周期资源", "石油石化": "周期资源", "建筑材料": "周期资源", "公用事业": "周期资源",
    "环保": "周期资源", "交通运输": "周期资源",
    "食品饮料": "大消费", "家用电器": "大消费", "纺织服饰": "大消费", "商贸零售": "大消费",
    "美容护理": "大消费", "社会服务": "大消费", "轻工制造": "大消费", "农林牧渔": "大消费",
    "医药生物": "医药",
    "国防军工": "高端制造", "机械设备": "高端制造", "汽车": "高端制造", "建筑装饰": "高端制造",
    "银行": "金融地产", "非银金融": "金融地产", "房地产": "金融地产", "综合": "综合",
    # 东财行业（港股兜底）→ 同主题分组
    "半导体": "泛科技TMT", "资讯科技器材": "泛科技TMT", "软件服务": "泛科技TMT",
    "电讯": "泛科技TMT", "媒体及娱乐": "泛科技TMT",
    "一般金属及矿石": "周期资源", "原材料": "周期资源", "黄金及贵金属": "周期资源",
    "石油及天然气": "周期资源", "建筑": "周期资源", "工用运输": "周期资源",
    "工用支援": "周期资源", "工业工程": "周期资源",
    "食物饮品": "大消费", "消费者主要零售商": "大消费", "专业零售": "大消费",
    "家庭电器及用品": "大消费", "纺织及服饰": "大消费", "旅游及消闲设施": "大消费",
    "支援服务": "大消费", "农业产品": "大消费",
    "药品及生物科技": "医药", "其他医疗保健": "医药",
    "银行": "金融地产", "保险": "金融地产", "其他金融": "金融地产", "地产": "金融地产",
    "公用事业": "周期资源",
    "综合企业": "综合",
}


def _theme_of(l1: str) -> str:
    return _THEME.get(l1 or "", "其他") if l1 else "未知"


def _agg_mix(top10: list, keyfn) -> dict:
    """把前十大按 keyfn（申万一级或大主题）聚合 → {mix, top, top_share, distinct}。

    share = 该组占前十大之比（剔除整体仓位高低，只看结构）；判断赛道看它。
    """
    agg: dict[str, float] = {}
    for h in top10:
        agg[keyfn(h)] = agg.get(keyfn(h), 0.0) + (h["hold_ratio"] or 0.0)
    total = sum(agg.values()) or 1.0
    mix = sorted(({"key": k, "ratio": _f(v), "share": round(v / total * 100, 1)}
                  for k, v in agg.items()), key=lambda x: -(x["ratio"] or 0))
    known = [m for m in mix if m["key"] not in ("未知", "其他")]
    top = known[0] if known else (mix[0] if mix else None)
    return {
        "mix": mix,
        "top": top["key"] if top else None,
        "top_share": top["share"] if top else None,
        "distinct": len([m for m in mix if m["key"] not in ("未知", "其他")]),
    }


def _sector_focus(dom_theme_seq: list[str], theme_shares: list[float],
                  l1_distincts: list[int], latest_share: float, latest_l1: int) -> str:
    """赛道定性线索（据持仓主题结构，非基金名）——**以最新季为主，跨季稳定性为辅**。

    规则优先级：最新横跨很广且主题不过半 → 选股型；同一主题长期居首且最新仍集中 → 赛道；
    主导主题跨季切换且各季集中 → 轮动。历史均值不再单独定调（会掩盖近期漂移）。
    """
    doms = [d for d in dom_theme_seq if d and d not in ("未知", "其他")]
    if not doms:
        return "未知"
    ls = latest_share or 0
    avg_share = sum(s for s in theme_shares if s) / (len(theme_shares) or 1)
    stable = len(set(doms)) == 1 and len(doms) >= len(dom_theme_seq) - 1  # 同一主题几乎每季居首
    # ① 最新季横跨很广（≥7 个一级）且最大主题不过半 → 当前就是选股型（即便历史曾集中）
    if (latest_l1 or 0) >= 7 and ls < 50:
        return "多行业分散(选股型)"
    if stable and ls >= 55:
        return "单一主题高度集中(赛道特征)"
    if stable and ls >= 42:
        return "单一主题主导(赛道倾向)"
    if len(set(doms)) >= 2 and avg_share >= 45:
        return "主题轮动(主导主题跨季切换)"
    if ls < 42 or (sum(l1_distincts) / max(1, len(l1_distincts))) >= 5:
        return "多行业分散(选股型)"
    return "较分散/混合"


def _holdings_block(conn, code: str) -> dict:
    rows = conn.execute(
        "SELECT h.quarter, h.asset_code, h.asset_name, h.hold_ratio, "
        "       COALESCE(NULLIF(si.sw_l1,''),'') AS sw_l1, "
        "       COALESCE(NULLIF(si.sw_l3,''),'') AS sw_l3, "
        "       COALESCE(NULLIF(si.em_industry,''),'') AS em_industry "
        "FROM fund_holdings h "
        "LEFT JOIN stock_industry si ON h.asset_code = si.stock_code "
        "WHERE h.fund_code=? AND h.holding_type='stock' AND h.hold_ratio IS NOT NULL "
        "ORDER BY h.quarter, h.hold_ratio DESC", (code,)).fetchall()
    if not rows:
        return {"available": False}
    by_q: dict[str, list] = {}
    for r in rows:
        by_q.setdefault(r["quarter"], []).append(r)

    quarters = []
    for q in sorted(by_q):
        hs = by_q[q]
        top10 = hs[:10]
        ratios = [h["hold_ratio"] for h in hs]
        n = len(hs)
        full = n > 15  # 半年报/年报=全部持仓；季报只前十大
        # 行业标签：申万一级优先，港股等无 sw_l1 时用东财行业兜底
        eff_l1 = lambda h: h["sw_l1"] or h["em_industry"] or ""
        li = _agg_mix(top10, lambda h: eff_l1(h) or "未知")
        th = _agg_mix(top10, lambda h: _theme_of(eff_l1(h)))
        mapped = len([h for h in top10 if eff_l1(h)])
        quarters.append({
            "quarter": q,
            "report_type": "全量(半年/年报)" if full else "前十大(季报)",
            "num_stock": n,
            "top1": {"name": hs[0]["asset_name"], "ratio": _f(ratios[0]),
                     "l1": eff_l1(top10[0]) or None},
            "top3_ratio": _f(sum(ratios[:3])),
            "top5_ratio": _f(sum(ratios[:5])),
            "top10_ratio": _f(sum(ratios[:10])),
            "hhi_top10": _f(sum((x / 100) ** 2 for x in ratios[:10])),  # 前十大赫芬达尔（0~1，越大越集中）
            # —— 行业结构（赛道判断的事实依据，不看基金名）——
            "sw_l1_mix": [{"l1": m["key"], "ratio": m["ratio"], "share": m["share"]} for m in li["mix"][:5]],
            "top_l1": li["top"], "top_l1_share": li["top_share"],
            "distinct_l1_top10": li["distinct"],       # 前十大横跨多少个申万一级
            "theme_mix": [{"theme": m["key"], "share": m["share"]} for m in th["mix"][:4]],
            "top_theme": th["top"], "top_theme_share": th["top_share"],  # 最大主题占前十大之比 %
            "l1_mapped_of_top10": mapped,              # 前十大里成功映射到行业的只数
            "codes_top10": [h["asset_code"] for h in top10],
        })

    # 相邻季前十大重合度（按股票代码），看换手/风格连续性
    overlaps = []
    for a, b in zip(quarters, quarters[1:]):
        sa, sb = set(a["codes_top10"]), set(b["codes_top10"])
        inter = sa & sb
        overlaps.append({"from": a["quarter"], "to": b["quarter"],
                         "overlap": len(inter), "of": min(len(sa), len(sb))})

    # 集中度趋势：用各季前十大占比（各季都有≥10只，可比）
    tr = [q["top10_ratio"] for q in quarters if q["top10_ratio"] is not None]
    trend = None
    if len(tr) >= 2:
        delta = tr[-1] - tr[0]
        trend = "上升" if delta > 5 else ("下降" if delta < -5 else "平稳")

    # 逐季主导「大主题」→ 稳定=赛道 / 切换=轮动 / 横跨多行业=选股型
    dominant_l1_seq = [q["top_l1"] for q in quarters]
    dominant_theme_seq = [q["top_theme"] for q in quarters]
    theme_shares = [q["top_theme_share"] or 0 for q in quarters]
    l1_distincts = [q["distinct_l1_top10"] or 0 for q in quarters]
    sector_focus = _sector_focus(dominant_theme_seq, theme_shares, l1_distincts,
                                 quarters[-1]["top_theme_share"] or 0,
                                 quarters[-1]["distinct_l1_top10"] or 0)
    # 主题集中度漂移：最新季 vs 最早季 最大主题占比之差
    theme_trend = None
    if len(theme_shares) >= 2 and theme_shares[0] and theme_shares[-1]:
        d = theme_shares[-1] - theme_shares[0]
        theme_trend = "收敛(更集中)" if d > 8 else ("发散(更分散)" if d < -8 else "稳定")

    # 精简季度输出：去掉 codes_top10（体积大，重合度已单列）
    for q in quarters:
        q.pop("codes_top10", None)

    # 最新一季前十大明细（名称+申万一级/三级+占比）——让 AI 看到具体持仓横跨哪些行业
    latest_rows = by_q[sorted(by_q)[-1]][:10]
    latest_detail = [{"name": h["asset_name"],
                      "l1": h["sw_l1"] or h["em_industry"] or "未知",
                      "theme": _theme_of(h["sw_l1"] or h["em_industry"]),
                      "l3": h["sw_l3"] or None,
                      "ratio": _f(h["hold_ratio"])}
                     for h in latest_rows]

    latest = quarters[-1]
    return {
        "available": True,
        "quarter_count": len(quarters),
        "coverage": f"{quarters[0]['quarter']}~{quarters[-1]['quarter']}",
        "quarters": quarters,
        "consecutive_top10_overlap": overlaps,
        "concentration_trend_top10": trend,
        # —— 行业结构派生（赛道判断以此为准，非基金名）——
        "dominant_l1_by_quarter": dominant_l1_seq,
        "dominant_theme_by_quarter": dominant_theme_seq,
        "distinct_dominant_theme": len({d for d in dominant_theme_seq if d and d not in ("未知", "其他")}),
        "theme_concentration_trend": theme_trend,   # 最大主题占比 收敛/发散/稳定
        "sector_focus": sector_focus,          # 赛道定性线索（据持仓主题结构，非基金名）
        "latest_top10_detail": latest_detail,
        "latest_top10_ratio": latest["top10_ratio"],
        "latest_top1": latest["top1"],
        "latest_top_theme": latest["top_theme"],
        "latest_top_theme_share": latest["top_theme_share"],
        "latest_distinct_l1": latest["distinct_l1_top10"],
    }


# ---------- 净值 regime（用累计收益曲线做粗归因）----------
def _return_between(series: list[tuple[str, float]], start: str | None) -> float | None:
    """区间收益：cum 为「成立以来累计收益%」，(1+R1)/(1+R0)-1。start 取最近的不早于它的点。"""
    if not series or not start:
        return None
    r1 = series[-1][1]
    r0 = None
    for d, v in series:
        if d >= start:
            r0 = v
            break
    if r0 is None:
        return None
    return _f(((1 + r1 / 100) / (1 + r0 / 100) - 1) * 100)


def _nav_regime_block(conn, code: str, mgr: dict) -> dict:
    rows = conn.execute(
        "SELECT trade_date, cum_return FROM fund_cum_return "
        "WHERE fund_code=? AND cum_return IS NOT NULL ORDER BY trade_date", (code,)).fetchall()
    series = [(r["trade_date"], r["cum_return"]) for r in rows]
    if len(series) < 2:
        return {"available": False}
    out = {
        "available": True,
        "coverage": f"{series[0][0]}~{series[-1][0]}",
        "points": len(series),
        "return_last_1y": _return_between(series, _years_ago(1)),
        "return_last_3y": _return_between(series, _years_ago(3)),
    }
    # 关键归因：当前团队上任以来的净收益（对齐经理任期）
    if mgr.get("available") and mgr.get("exact_team_since"):
        out["return_since_current_team"] = _return_between(series, mgr["exact_team_since"])
    return out


# ---------- 组装 ----------
def build_bundle(code: str) -> dict | None:
    detail = database.select_one("fund_details", {"fund_code": f"eq.{code}"})
    fund = database.select_one("funds", {"code": f"eq.{code}"})
    if not detail and not fund:
        return None
    detail = detail or {}
    conn = database.get_db()._conn()  # pylint: disable=protected-access

    mgr = _manager_block(code)
    caveats = []
    hold = _holdings_block(conn, code)
    if hold.get("available") and hold["coverage"].startswith("2025"):
        caveats.append("持仓历史仅覆盖 2025Q1 起（约 1.25 年），更早季度库内无数据")
    if detail.get("sharpe_5y") is None:
        caveats.append("无 5 年指标（多为 2022 年后成立的 C 类）")
    if mgr.get("available") and mgr.get("distinct_team_count", 1) > 1:
        caveats.append(f"历史换过 {mgr['distinct_team_count']} 个不同团队；长周期指标未必归当前团队")

    return {
        "meta": {
            "code": code,
            "name": detail.get("fund_name") or (fund or {}).get("name"),
            "full_name": detail.get("fund_full_name"),
            "company": detail.get("fund_company"),
            "type": detail.get("fund_type") or (fund or {}).get("fund_type"),
            "establish_date": detail.get("establish_date"),
            "scale_yi": detail.get("scale"),
            "invest_strategy": _clip(detail.get("invest_strategy"), 160),
        },
        "metrics": {
            "return": {k: detail.get(f"return_{k}") for k in ("ytd", "1y", "3y", "5y", "since_inception")},
            "sharpe": {k: detail.get(f"sharpe_{k}") for k in ("1y", "3y", "5y")},
            "max_drawdown": {k: detail.get(f"max_drawdown_{k}") for k in ("ytd", "1y", "3y", "5y")},
            "volatility": {k: detail.get(f"volatility_{k}") for k in ("1y", "3y", "5y")},
            "rank": {k: detail.get(f"rank_{k}") for k in ("1y", "3y", "5y")},
            "position_stock": detail.get("position_stock"),
            "position_bond": detail.get("position_bond"),
        },
        "manager": mgr,
        "holdings": hold,
        "nav_regime": _nav_regime_block(conn, code, mgr),
        "data_caveats": caveats,
    }


def cmd_bundle(args) -> None:
    codes = [c.strip() for c in (args.code or "").split(",") if c.strip()]
    if not codes:
        print("需 --code（逗号可多只）", file=sys.stderr)
        sys.exit(1)
    bundles = [b for b in (build_bundle(c) for c in codes) if b]
    payload = bundles[0] if len(codes) == 1 else bundles
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
