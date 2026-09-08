#!/usr/bin/env python3
"""对生产 MySQL 幂等执行 Tushare 基金 Phase 3 additive DDL。"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pymysql

BACKEND = Path(__file__).resolve().parents[1]
TABLES = ("fund_company", "fund_basic_ext", "fund_share", "fund_share_sync_state")


def _statements(sql_text: str) -> list[str]:
    """剥离整行注释并按分号切分本项目的简单 DDL。"""
    clean = "\n".join(
        line for line in sql_text.splitlines() if not line.lstrip().startswith("--")
    )
    return [statement.strip() for statement in clean.split(";") if statement.strip()]


def main() -> int:
    """创建四张 additive 表，不执行删除或核心表 UPDATE。"""
    if os.getenv("DB_BACKEND", "").lower() != "mysql":
        raise RuntimeError("Phase 3 DDL 仅允许 DB_BACKEND=mysql")
    ddl_path = BACKEND / "docs" / "tushare-fund-phase3.sql"
    statements = _statements(ddl_path.read_text(encoding="utf-8"))
    connection = pymysql.connect(
        host=os.environ["IFUND_DB_HOST"],
        port=int(os.environ.get("IFUND_DB_PORT", "3306")),
        user=os.environ["IFUND_DB_USER"],
        password=os.environ["IFUND_DB_PASSWORD"],
        database=os.environ["IFUND_DB_NAME"],
        charset="utf8mb4",
        autocommit=True,
    )
    try:
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
            cursor.execute("SELECT @@collation_database")
            database_collation = str(cursor.fetchone()[0])
            if not re.fullmatch(r"[A-Za-z0-9_]+", database_collation):
                raise RuntimeError("生产库默认 collation 格式异常")
            cursor.execute(
                "SELECT table_name,table_collation FROM information_schema.tables "
                "WHERE table_schema=%s AND table_name IN (%s,%s,%s,%s)",
                (os.environ["IFUND_DB_NAME"], *TABLES),
            )
            collations = {str(row[0]): str(row[1]) for row in cursor.fetchall()}
            for table in TABLES:
                if collations.get(table) != database_collation:
                    cursor.execute(
                        f"ALTER TABLE `{table}` CONVERT TO CHARACTER SET utf8mb4 "
                        f"COLLATE {database_collation}"
                    )
    finally:
        connection.close()
    print({
        "ddl": "ok",
        "tables": len(TABLES),
        "collation": database_collation,
        "path": str(ddl_path),
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
