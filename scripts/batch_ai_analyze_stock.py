#!/usr/bin/env python3
"""股票型基金批量 AI 定性分析（并发 + 重试 + 幂等）。

用法：
    python3 scripts/batch_ai_analyze_stock.py            # 分析全部股票型（跳过已分析）
    python3 scripts/batch_ai_analyze_stock.py --limit 50 # 前 50 只试跑
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app import db as database
from app.ai_analyze.service import analyze_fund

# 目标基金类型（可多个）；指数型含标准指数 + 增强指数
FUND_TYPES = ["股票型-普通", "股票型-标准指数", "股票型-增强指数"]
FUND_TYPES = os.environ.get("IFUND_AI_FUND_TYPES", "|".join(FUND_TYPES)).split("|")
CONCURRENCY = int(os.environ.get("IFUND_AI_CONCURRENCY", "4"))
MAX_RETRIES = 2


def get_stock_fund_codes(limit: int | None = None) -> list[str]:
    # 支持 A 层过滤：IFUND_AI_MIN_SCALE(亿) + IFUND_AI_MIN_STOCK_PCT 环境变量
    min_scale = float(os.environ.get("IFUND_AI_MIN_SCALE", "0"))
    min_stock = float(os.environ.get("IFUND_AI_MIN_STOCK_PCT", "0"))
    codes: list[str] = []
    for ft in FUND_TYPES:
        rows = database.select("fund_details", [("fund_type", f"eq.{ft}")])
        for r in rows:
            if min_scale and (r.get("scale") or 0) < min_scale:
                continue
            if min_stock and (r.get("position_stock") or 0) < min_stock:
                continue
            codes.append(r["fund_code"])
    if limit:
        codes = codes[:limit]
    return codes


def load_analyzed() -> set[str]:
    rows = database.select("fund_ai_analysis", [("select", "fund_code")])
    return {r["fund_code"] for r in rows}


def analyze_one(code: str) -> tuple[str, str, float, dict]:
    """返回 (code, status, elapsed, info)。status: ok/error/skip。"""
    t0 = time.time()
    fund = database.select_one("fund_details", [("fund_code", f"eq.{code}")])
    name = fund["fund_name"] if fund else code
    last_err = ""
    for attempt in range(1 + MAX_RETRIES):
        try:
            ai = analyze_fund(code)
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
            return code, "ok", time.time() - t0, {"name": name, "rating": ai.get("rating"), "attempts": attempt + 1}
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            if attempt < MAX_RETRIES:
                time.sleep(3 * (attempt + 1))
    return code, "error", time.time() - t0, {"name": name, "error": last_err, "attempts": 1 + MAX_RETRIES}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=CONCURRENCY)
    ap.add_argument("--force", action="store_true", help="重跑已分析基金")
    args = ap.parse_args()

    all_codes = get_stock_fund_codes(args.limit)
    if not args.force:
        analyzed = load_analyzed()
        todo = [c for c in all_codes if c not in analyzed]
        print(f"[batch] 类型 {FUND_TYPES} 共 {len(all_codes)} 只，已分析 {len(all_codes) - len(todo)} 只，待分析 {len(todo)} 只", flush=True)
    else:
        todo = all_codes
        print(f"[batch] 强制重跑 {len(todo)} 只", flush=True)

    if not todo:
        print("[batch] 无待分析基金，退出", flush=True)
        return

    t_start = time.time()
    ok = err = 0
    done = 0
    errors: list[tuple[str, str, str]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = {ex.submit(analyze_one, c): c for c in todo}
        for fut in concurrent.futures.as_completed(futs):
            code, status, elapsed, info = fut.result()
            done += 1
            if status == "ok":
                ok += 1
                print(f"[{done}/{len(todo)}] OK {code} {info['name']} ★{info['rating']} ({elapsed:.0f}s, 尝试{info['attempts']})", flush=True)
            else:
                err += 1
                errors.append((code, info["name"], info["error"]))
                print(f"[{done}/{len(todo)}] ERR {code} {info['name']}: {info['error']}", flush=True)

    total = time.time() - t_start
    print(f"\n[batch] 完成: {ok} 成功 / {err} 失败 / {len(todo)} 总，耗时 {total/60:.1f} 分钟", flush=True)
    if errors:
        print("[batch] 失败清单:", flush=True)
        for code, name, e in errors:
            print(f"  {code} {name}: {e}", flush=True)


if __name__ == "__main__":
    main()
