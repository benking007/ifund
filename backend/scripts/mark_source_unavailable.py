#!/usr/bin/env python3
"""把蛋卷源确认不收录的基金标记为「源不可用」占位记录。

背景：danjuanfunds djapi 对部分基金（后端收费份额、定期开放债、部分 FOF/联接/
货币 B 等）不返回有效数据（akshare 抛 KeyError 'data'）。若不做标记，is_expired
对这些无记录基金永远 True → 每天 cron 空拉 4 接口 × N 只，浪费且拖慢整体。

本脚本对给定清单写入占位记录：
  fund_details 单行（若已有真实记录则跳过）：
    detail_json = {"source_unavailable": true}
    fetch_time  = 当前时间（is_expired 7 天内跳过）
    trade_date  = 当前基准交易日

占位记录 7 天后自动过期重探——蛋卷若补录该基金则自然恢复拉取。

用法：
  venv/bin/python3.12 scripts/mark_source_unavailable.py --codes-file /tmp/codes.txt
  venv/bin/python3.12 scripts/mark_source_unavailable.py --codes 000002,000012
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)

from app import db as database  # noqa: E402
from app.fund_detail.crud import detail_crud  # noqa: E402
from app.trade_calendar.crud import calendar_crud  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codes", help="基金代码，逗号分隔")
    parser.add_argument("--codes-file", help="基金代码文件（逗号分隔或每行一个）")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写入")
    args = parser.parse_args()

    codes: list[str] = []
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    elif args.codes_file:
        raw = Path(args.codes_file).read_text()
        codes = [c.strip() for c in raw.replace("\n", ",").split(",") if c.strip()]
    if not codes:
        parser.error("需要 --codes 或 --codes-file")
    codes = list(dict.fromkeys(codes))

    trade_date = calendar_crud.base_trade_date()
    now = datetime.datetime.now().isoformat(timespec="seconds")
    marked = skipped = 0
    for code in codes:
        row = detail_crud.get_detail(code)
        if row and not (row.get("detail_json") or "").startswith('{"source_unavailable"'):
            skipped += 1  # 已有真实记录，不动
            continue
        if args.dry_run:
            marked += 1
            continue
        columns = {
            "detail_json": json.dumps({"source_unavailable": True}),
            "fetch_time": now,
            "trade_date": trade_date,
        }
        detail_crud.upsert(code, columns)
        marked += 1

    print(
        f"标记完成: 写入 {marked} 只占位记录, 跳过已有真实记录 {skipped} 只, "
        f"基准日 {trade_date}" + ("（dry-run）" if args.dry_run else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
