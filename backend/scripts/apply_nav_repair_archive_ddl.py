#!/usr/bin/env python3
"""放宽 nav_repair_queue.status 的 CHECK，允许 archived。

只修改约束，不更新、不删除业务行。生产执行前请先 dry-run 阅读将要执行的语句。
需要环境变量 IFUND_DB_HOST / IFUND_DB_USER / IFUND_DB_PASSWORD / IFUND_DB_NAME。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pymysql

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.fund_nav.repair_triage import plan_status_check_ddl  # noqa: E402 pylint: disable=wrong-import-position


def _checks(cursor, schema: str) -> list[dict]:
    cursor.execute(
        "SELECT cc.CONSTRAINT_NAME AS name, cc.CHECK_CLAUSE AS clause "
        "FROM information_schema.CHECK_CONSTRAINTS cc "
        "JOIN information_schema.TABLE_CONSTRAINTS tc "
        "ON tc.CONSTRAINT_SCHEMA=cc.CONSTRAINT_SCHEMA "
        "AND tc.CONSTRAINT_NAME=cc.CONSTRAINT_NAME "
        "WHERE tc.TABLE_SCHEMA=%s AND tc.TABLE_NAME='nav_repair_queue'",
        (schema,),
    )
    return list(cursor.fetchall())


def main() -> int:
    """连接 MySQL 并执行约束变更。"""
    required = ("IFUND_DB_HOST", "IFUND_DB_USER", "IFUND_DB_PASSWORD", "IFUND_DB_NAME")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        print("缺少环境变量: " + ",".join(missing), file=sys.stderr)
        return 2
    schema = os.environ["IFUND_DB_NAME"]
    connection = pymysql.connect(
        host=os.environ["IFUND_DB_HOST"],
        port=int(os.environ.get("IFUND_DB_PORT", "3306")),
        user=os.environ["IFUND_DB_USER"],
        password=os.environ["IFUND_DB_PASSWORD"],
        database=schema,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )
    try:
        with connection.cursor() as cursor:
            statements = plan_status_check_ddl(_checks(cursor, schema))
            if not statements:
                print("SKIP nav_repair_queue status check already allows archived")
                return 0
            for statement in statements:
                print(statement)
                cursor.execute(statement)
    finally:
        connection.close()
    print("DDL OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
