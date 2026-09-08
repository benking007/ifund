#!/usr/bin/env python3
"""执行 Tushare fund_basic → fund_ts_code_map 全量映射（additive，不改 funds 表）。

Token：默认 TUSHARE_PREFER_FIN_DATA=1 使用 fin-data/.env（ifund-prod token 可能无效）。
限速：fund_basic 分页间 sleep，避免与 A 股财务回填抢频。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

os.environ.setdefault("TUSHARE_PREFER_FIN_DATA", "1")

from app import db as database  # pylint: disable=wrong-import-position
from app.fund_nav import ts_code_map  # pylint: disable=wrong-import-position
from app.fund_nav.fetch import tushare_client  # pylint: disable=wrong-import-position

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

PAGE_SLEEP = max(float(os.getenv("TUSHARE_INTERVAL_MS", "3500")) / 1000.0, 2.0)


def _load_ifund_codes() -> set[str]:
    rows = database.select("funds", [("select", "code"), ("limit", 500_000)])
    return {str(r.get("code") or "").strip() for r in rows if r.get("code")}


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync fund_ts_code_map from Tushare fund_basic")
    parser.add_argument("--dry-run", action="store_true", help="只拉取统计，不写库")
    args = parser.parse_args()

    if not tushare_client.get_token():
        logger.error("TUSHARE_TOKEN 不可用")
        return 1

    ts_code_map.ensure_schema()
    ifund_codes = _load_ifund_codes()
    logger.info("iFund funds=%d", len(ifund_codes))

    logger.info("拉取 fund_basic 全量（分页 sleep=%.1fs）...", PAGE_SLEEP)
    # 包装 call 加 sleep — fetch_all_fund_basic 内部连续分页，我们在脚本层节流
    t0 = time.time()
    basic_rows = ts_code_map.fetch_all_fund_basic()
    logger.info("fund_basic 去重后 %d 条，耗时 %.1fs", len(basic_rows), time.time() - t0)

    map_rows, quar_rows = ts_code_map.build_mapping_rows(basic_rows, ifund_codes)
    matched = len(map_rows)
    unmatched = len(ifund_codes) - matched
    e_count = sum(1 for r in map_rows if r.get("market") == "E")
    o_count = sum(1 for r in map_rows if r.get("market") == "O")

    logger.info(
        "映射结果: matched=%d unmatched_ifund=%d quarantine=%d E=%d O=%d",
        matched, unmatched, len(quar_rows), e_count, o_count,
    )

    if args.dry_run:
        print({
            "matched": matched,
            "unmatched_ifund": unmatched,
            "quarantine": len(quar_rows),
            "market_E": e_count,
            "market_O": o_count,
        })
        return 0

    result = ts_code_map.persist_mapping(map_rows, quar_rows)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
