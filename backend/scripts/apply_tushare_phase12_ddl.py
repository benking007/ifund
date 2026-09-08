#!/usr/bin/env python3
"""对生产 MySQL 执行 Phase 1+2 additive DDL（映射表 + fund_div_split 扩展列）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pymysql

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


def main() -> int:
    host = os.environ["IFUND_DB_HOST"]
    port = int(os.environ.get("IFUND_DB_PORT", "3306"))
    user = os.environ["IFUND_DB_USER"]
    password = os.environ["IFUND_DB_PASSWORD"]
    dbname = os.environ["IFUND_DB_NAME"]

    ddl_path = BACKEND / "docs" / "tushare-fund-phase12.sql"
    base_sql = ddl_path.read_text(encoding="utf-8")

    extra_cols = [
        ("ts_code", "VARCHAR(20) NULL COMMENT 'Tushare ts_code'"),
        ("ann_date", "VARCHAR(10) NULL COMMENT '公告日'"),
        ("pay_date", "VARCHAR(10) NULL COMMENT '派息日'"),
        ("record_date", "VARCHAR(10) NULL COMMENT '权益登记日'"),
    ]

    conn = pymysql.connect(
        host=host, port=port, user=user, password=password,
        database=dbname, charset="utf8mb4", autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            for stmt in base_sql.split(";"):
                stmt = stmt.strip()
                if stmt and not stmt.startswith("--"):
                    cur.execute(stmt)
            cur.execute(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='fund_div_split'",
                (dbname,),
            )
            existing = {r[0] for r in cur.fetchall()}
            for col, typedef in extra_cols:
                if col not in existing:
                    cur.execute(f"ALTER TABLE fund_div_split ADD COLUMN `{col}` {typedef}")
                    print(f"ADD COLUMN fund_div_split.{col}")
                else:
                    print(f"SKIP fund_div_split.{col} exists")
    finally:
        conn.close()
    print("DDL OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
