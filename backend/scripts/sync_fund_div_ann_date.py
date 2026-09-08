#!/usr/bin/env python3
"""按 ann_date 窗口批量同步 fund_div → fund_div_split（低调用量策略）。

默认 3s/次（≈20/min），复用 fund_sync_state(task_kind=div_ann_date) 水位。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

os.environ.setdefault("TUSHARE_PREFER_FIN_DATA", "1")

from app.fund_nav import div_sync  # pylint: disable=wrong-import-position
from app.fund_nav import ts_code_map  # pylint: disable=wrong-import-position
from app.fund_nav.fetch import tushare_client  # pylint: disable=wrong-import-position

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill fund_div by ann_date window")
    parser.add_argument("--start", help="起始 ISO 日期 YYYY-MM-DD")
    parser.add_argument("--end", help="结束 ISO 日期 YYYY-MM-DD")
    parser.add_argument("--limit-days", type=int, help="最多处理天数（小批验证）")
    parser.add_argument("--interval-ms", type=int, default=3000, help="请求间隔毫秒")
    args = parser.parse_args()

    if not tushare_client.get_token():
        logger.error("TUSHARE_TOKEN 不可用")
        return 1

    ts_code_map.ensure_schema()
    interval = max(args.interval_ms / 1000.0, 2.0)
    os.environ["TUSHARE_INTERVAL_MS"] = str(args.interval_ms)

    result = div_sync.run_backfill(
        start_date=args.start,
        end_date=args.end,
        limit_days=args.limit_days,
        interval=interval,
    )
    print(result)
    return 0 if not result.get("errors") else 2


if __name__ == "__main__":
    raise SystemExit(main())
