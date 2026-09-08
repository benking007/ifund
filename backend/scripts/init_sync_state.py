#!/usr/bin/env python3
"""从 fund_nav 聚合结果初始化净值同步水位；不创建任何表。"""

from __future__ import annotations

# 初始化脚本需要一条通用 CRUD DSL 不支持的 GROUP BY 聚合 SQL。
# pylint: disable=duplicate-code,protected-access,wrong-import-position
import argparse
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - backend requirements normally provide it
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv(BACKEND_DIR / ".env")

from app import db as database
from app.db.mysql import MysqlDatabase
from app.fund_nav import sync_state


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须为正整数")
    return parsed


def load_source_watermarks(limit: int | None = None) -> dict[str, object]:
    """一次聚合查询读取每只基金的最新净值日期。"""
    db = database.get_db()
    if not isinstance(db, MysqlDatabase):
        raise TypeError("init_sync_state 仅支持 DB_BACKEND=mysql")
    sql = (
        "SELECT `fund_code`, MAX(`trade_date`) AS `watermark_date` "
        "FROM `fund_nav` GROUP BY `fund_code` ORDER BY `fund_code`"
    )
    params: list[int] = []
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    with db._connection() as connection, connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall() or []
    return {
        str(row["fund_code"]).strip(): row["watermark_date"]
        for row in rows
        if row.get("fund_code") and row.get("watermark_date")
    }


def run_init(*, dry_run: bool = False, limit: int | None = None) -> dict:
    """统计并按需批量回填水位。"""
    watermarks = load_source_watermarks(limit)
    backfilled = 0
    if not dry_run:
        backfilled = sync_state.set_watermarks("nav", watermarks)
    return {
        "candidates": len(watermarks),
        "backfilled": backfilled,
        "dry_run": dry_run,
    }


def build_parser() -> argparse.ArgumentParser:
    """构造水位初始化命令行参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="只统计，不写 fund_sync_state"
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON 统计")
    parser.add_argument("--limit", type=_positive_int, help="最多回填前 N 只基金")
    return parser


def main(argv: list[str] | None = None) -> int:
    """运行水位初始化并按指定格式输出统计。"""
    args = build_parser().parse_args(argv)
    result = run_init(dry_run=args.dry_run, limit=args.limit)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(
            f"候选={result['candidates']} 回填={result['backfilled']} "
            f"dry_run={result['dry_run']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
