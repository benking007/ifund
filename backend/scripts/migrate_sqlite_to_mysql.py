#!/usr/bin/env python3
"""全量迁移 iFund SQLite data.db → MySQL（192.168.0.9/ifund）。

源：SQLite data.db（读）
目标：MySQL ifund 库（写，凭据 /etc/ifund-prod.env 或环境变量）

策略：
- 小表一次性；大表（fund_nav/fund_cum_return/fund_holdings）按主键分片游标，
  每片 batch_size 行用 executemany 写入，落进度到 checkpoint 文件，断点续跑。
- 幂等：每表开始前 TRUNCATE 目标表；重复执行安全。
- 完成后输出各行数对比（源 vs 目标）供对账。

用法：
  set -a; . /etc/ifund-prod.env; set +a
  venv/bin/python3.12 scripts/migrate_sqlite_to_mysql.py [--batch 5000] [--table fund_nav]
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)

from app.db.sqlite import SqliteDatabase  # noqa: E402
from app.db.mysql import MysqlDatabase  # noqa: E402

# 迁移顺序：先小表后大表；fund_nav 最后（最大）
TABLES = [
    "users", "api_tokens", "app_settings", "fund_types", "funds",
    "query_presets", "fund_snapshots", "trade_dates", "stock_industry",
    "fund_div_split", "fetch_tasks", "event_scan_status", "nav_repair_queue",
    "fund_details", "fund_ai_analysis", "fund_etf_linkage", "fund_manager_tenure",
    "portfolios", "user_holdings", "holding_txns", "perpetual_portfolio",
    "fund_holdings", "fund_cum_return", "fund_nav",
]

CHECKPOINT = Path("/tmp/ifund_migrate_checkpoint.txt")


def _src() -> SqliteDatabase:
    return SqliteDatabase(os.getenv("IFUND_SQLITE_PATH") or "data.db")


def _dst() -> MysqlDatabase:
    return MysqlDatabase()


def _table_rows(src: SqliteDatabase, table: str) -> int:
    row = src._conn().execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0]) if row else 0


def _migrate_small(src: SqliteDatabase, dst: MysqlDatabase, table: str) -> int:
    rows = src.select(table)
    if not rows:
        return 0
    dst.batch_insert(table, rows)
    return len(rows)


def _migrate_large(
    src: SqliteDatabase, dst: MysqlDatabase, table: str, batch: int
) -> int:
    """大表按 id 分片游标迁移。"""
    src_conn = src._conn()
    total = _table_rows(src, table)
    done = 0
    last_id = 0
    while True:
        batch_rows = src_conn.execute(
            f"SELECT * FROM {table} WHERE id > ? ORDER BY id LIMIT ?",
            (last_id, batch),
        ).fetchall()
        if not batch_rows:
            break
        last_id = batch_rows[-1][0]
        rows = [dict(r) for r in batch_rows]
        dst.batch_insert(table, rows)
        done += len(rows)
        if done % (batch * 10) == 0 or done == total:
            print(
                f"  {table}: {done:,}/{total:,} ({done / total * 100:.1f}%)",
                flush=True,
            )
    return done


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=int, default=5000)
    parser.add_argument("--table", help="只迁移指定表")
    args = parser.parse_args()

    src, dst = _src(), _dst()
    tables = [args.table] if args.table else TABLES
    summary = {}
    started = datetime.datetime.now()

    for table in tables:
        if CHECKPOINT.exists() and args.table is None:
            done_tables = CHECKPOINT.read_text().split()
            if table in done_tables:
                print(f"跳过已完成的 {table}（checkpoint）", flush=True)
                continue
        print(f"=== 迁移 {table} ===", flush=True)
        t0 = time.time()
        dst.delete(table)  # 幂等：清目标表
        count = _table_rows(src, table)
        if count < 200_000:
            moved = _migrate_small(src, dst, table)
        else:
            moved = _migrate_large(src, dst, table, args.batch)
        summary[table] = (count, moved)
        if moved != count:
            print(
                f"  WARN {table}: 源 {count:,} 目标 {moved:,} 不一致",
                flush=True,
            )
        with CHECKPOINT.open("a") as f:
            f.write(f"{table}\n")
        print(f"  {table} 完成 {moved:,} 行 ({time.time() - t0:.0f}s)", flush=True)

    elapsed = (datetime.datetime.now() - started).total_seconds()
    print("\n=== 迁移汇总 ===")
    ok = True
    for table, (src_n, dst_n) in summary.items():
        match = src_n == dst_n
        ok = ok and match
        print(f"  {table}: 源 {src_n:,} 目标 {dst_n:,} {'OK' if match else 'MISMATCH'}")
    print(f"总耗时 {elapsed:.0f}s，{'全部一致' if ok else '存在不一致！'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
