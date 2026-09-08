#!/usr/bin/env python3
"""回填指数 ETF 联接基金 ↔ 场内 ETF 血缘（幂等 upsert，可重复执行）。

扫描 ``funds.name`` 含「联接」的基金（含 ``ETF联接`` / ``ETF发起式联接`` /
短名省略 ETF 的联接，如 011609 易方达上证科创50联接C），按公司词 + 指数
关键词匹配场内 ETF，再用 fund_details.benchmark / invest_target 交叉验证。

用法::

    python3 scripts/backfill_etf_linkage.py
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv(BACKEND_DIR / ".env")

from app import db as database
from app.fund_etf_linkage import crud
from app.fund_etf_linkage.parser import (
    collect_company_tokens,
    is_etf_name,
    is_feeder_name,
    match_feeder_to_etf,
)


def _load_details(codes: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for offset in range(0, len(codes), 200):
        chunk = codes[offset : offset + 200]
        rows = database.select(
            "fund_details",
            {
                "fund_code": f"in.({','.join(chunk)})",
            },
        )
        for row in rows:
            out[row["fund_code"]] = row
    return out


def run() -> dict:
    schema_sql = (BACKEND_DIR / "schema_sqlite.sql").read_text(encoding="utf-8")
    database.init_db(schema_sql)
    funds = database.select("funds", {"select": "code,name"})
    feeders = [row for row in funds if is_feeder_name(row.get("name") or "")]
    etfs = [row for row in funds if is_etf_name(row.get("name") or "")]
    details = _load_details([row["code"] for row in feeders])
    tokens = collect_company_tokens(
        etf_names=[row["name"] for row in etfs],
        fund_companies=[
            (details.get(row["code"]) or {}).get("fund_company") or ""
            for row in feeders
        ],
    )

    matched: list[dict] = []
    unmatched: list[dict] = []
    for feeder in feeders:
        code = feeder["code"]
        name = feeder["name"]
        detail = details.get(code) or {}
        hit = match_feeder_to_etf(
            name,
            etfs,
            company_tokens=tokens,
            benchmark=detail.get("benchmark") or "",
            invest_target=detail.get("invest_target") or "",
            fund_company=detail.get("fund_company") or "",
        )
        if hit and hit.etf_code:
            matched.append(
                {
                    "fund_code": code,
                    "fund_name": name,
                    "etf_code": hit.etf_code,
                    "etf_name": hit.etf_name,
                    "matched_by": hit.matched_by,
                    "confidence": hit.confidence,
                }
            )
        else:
            unmatched.append({"fund_code": code, "fund_name": name})

    with database.get_db().transaction():
        for row in matched:
            crud.upsert_linkage(row)
        for row in unmatched:
            if crud.get_linkage(row["fund_code"]):
                crud.delete_linkage(row["fund_code"])

    conf = Counter(row["confidence"] for row in matched)
    by = Counter(row["matched_by"] for row in matched)
    stats = {
        "feeders": len(feeders),
        "etfs": len(etfs),
        "matched": len(matched),
        "unmatched": len(unmatched),
        "confidence": dict(conf),
        "matched_by": dict(by),
        "unmatched_sample": unmatched[:20],
    }
    return stats


def main() -> int:
    stats = run()
    print(
        f"feeders={stats['feeders']} etfs={stats['etfs']} "
        f"matched={stats['matched']} unmatched={stats['unmatched']}"
    )
    print(f"confidence={stats['confidence']}")
    print(f"matched_by={stats['matched_by']}")
    print("unmatched sample:")
    for row in stats["unmatched_sample"]:
        print(f"  {row['fund_code']} {row['fund_name']}")
    if stats["unmatched"] > len(stats["unmatched_sample"]):
        print(f"  ... +{stats['unmatched'] - len(stats['unmatched_sample'])} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
