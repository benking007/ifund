"""CLI: AI 定性分析批量执行。"""
from __future__ import annotations

import json
import sys
import time


def cmd_ai_batch(args) -> None:
    """批量 AI 定性分析。

    用法：
        ifund ai-analyze batch --codes 015383,002451
        ifund ai-analyze batch --preset 1   # 分析预设镜像全部基金
    """
    from app import db as database
    from app.ai_analyze.service import analyze_fund

    codes: list[str] = []
    if getattr(args, "codes", None):
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    elif getattr(args, "preset", None):
        rows = database.select("mirror_fund", [("preset_id", f"eq.{args.preset}")])
        codes = [r["fund_code"] for r in rows]
    else:
        print("错误：需指定 --codes 或 --preset", file=sys.stderr)
        sys.exit(1)

    if not codes:
        print("无基金需要分析", file=sys.stderr)
        sys.exit(0)

    print(f"开始分析 {len(codes)} 只基金...", flush=True)
    results = []
    for i, code in enumerate(codes, 1):
        fund = database.select_one("fund_details", [("fund_code", f"eq.{code}")])
        name = fund["fund_name"] if fund else code
        print(f"[{i}/{len(codes)}] {code} {name} ...", end=" ", flush=True)
        t0 = time.time()
        try:
            ai = analyze_fund(code)
            elapsed = time.time() - t0
            fields = {
                "verdict": ai.get("verdict"),
                "rating": ai.get("rating"),
                "recommend": ai.get("recommend"),
                "skill_score": ai.get("skill_score"),
                "luck_verdict": ai.get("luck_verdict"),
                "skill_reason": ai.get("skill_reason"),
                "concentration": ai.get("concentration"),
                "concentration_reason": ai.get("concentration_reason"),
                "fund_kind": ai.get("fund_kind"),
                "hard_thesis": ai.get("hard_thesis"),
                "manager": ai.get("manager"),
                "tenure_years": ai.get("tenure_years"),
                "is_original": ai.get("is_original"),
                "is_comanaged": ai.get("is_comanaged"),
                "scale_risk": ai.get("scale_risk"),
                "style_stability": ai.get("style_stability"),
                "turnover_note": ai.get("turnover_note"),
                "tags": json.dumps(ai.get("tags"), ensure_ascii=False) if ai.get("tags") else None,
                "confidence": ai.get("confidence"),
                "model": ai.get("model"),
                "data_basis": ai.get("data_basis"),
            }
            exists = database.select_one("fund_ai_analysis", [("fund_code", f"eq.{code}")])
            if exists:
                database.update("fund_ai_analysis", {"fund_code": code}, fields)
            else:
                database.insert("fund_ai_analysis", {"fund_code": code, **fields})
            print(f"★{ai.get('rating')} 实力{ai.get('skill_score')} {ai.get('luck_verdict')} ({elapsed:.0f}s)")
            results.append({"code": code, "status": "ok", "rating": ai.get("rating")})
        except Exception as e:
            print(f"失败: {e}")
            results.append({"code": code, "status": "error", "error": str(e)})

    ok = sum(1 for r in results if r["status"] == "ok")
    print(f"\n完成: {ok}/{len(codes)} 成功")
    if args.json:
        print(json.dumps(results, ensure_ascii=False))
