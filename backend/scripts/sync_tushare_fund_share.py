#!/usr/bin/env python3
"""按基金分页、幂等并可续跑地同步 Tushare fund_share。"""
from __future__ import annotations

# 两个独立运维入口刻意保留相同的安全连接前置，便于单文件执行。
# pylint: disable=duplicate-code

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

import pymysql

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from app.fund_reference import tushare_sync  # pylint: disable=wrong-import-position


def _connect():
    return pymysql.connect(
        host=os.environ["IFUND_DB_HOST"],
        port=int(os.environ.get("IFUND_DB_PORT", "3306")),
        user=os.environ["IFUND_DB_USER"],
        password=os.environ["IFUND_DB_PASSWORD"],
        database=os.environ["IFUND_DB_NAME"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def _load_targets(
    cursor,
    codes: list[str] | None,
    resume: bool,
    requested_start: str | None,
    requested_end: str,
) -> list[dict]:
    where: list[str] = []
    params: list[object] = []
    if codes:
        where.append("m.fund_code IN (" + ",".join(["%s"] * len(codes)) + ")")
        params.extend(codes)
    if resume:
        where.append(
            "(COALESCE(s.status, '') <> 'success' "
            "OR (%s IS NOT NULL AND (s.requested_start IS NULL OR s.requested_start > %s)) "
            "OR s.requested_end IS NULL OR s.requested_end < %s)"
        )
        params.extend([requested_start, requested_start, requested_end])
    sql = (
        "SELECT m.fund_code,m.ts_code FROM fund_ts_code_map m "
        "LEFT JOIN fund_share_sync_state s ON s.ts_code=m.ts_code"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY m.fund_code"
    cursor.execute(sql, params)
    return list(cursor.fetchall())


def _upsert_shares(cursor, rows: list[dict]) -> None:
    if not rows:
        return
    columns = list(rows[0])
    names = ",".join(f"`{column}`" for column in columns)
    values = ",".join(["%s"] * len(columns))
    updates = ",".join(
        f"`{column}`=VALUES(`{column}`)"
        for column in columns
        if column not in {"ts_code", "trade_date", "share_type"}
    )
    cursor.executemany(
        f"INSERT INTO fund_share ({names}) VALUES ({values}) "
        f"ON DUPLICATE KEY UPDATE {updates}",
        [[row.get(column) for column in columns] for row in rows],
    )


def _state(
    cursor,
    ts_code: str,
    start: str | None,
    end: str | None,
    status: str,
    row_count: int,
    error: str | None = None,
) -> None:
    cursor.execute(
        "INSERT INTO fund_share_sync_state "
        "(ts_code,requested_start,requested_end,status,row_count,attempts,last_error) "
        "VALUES (%s,%s,%s,%s,%s,1,%s) ON DUPLICATE KEY UPDATE "
        "requested_start=VALUES(requested_start),requested_end=VALUES(requested_end),"
        "status=VALUES(status),row_count=VALUES(row_count),attempts=attempts+1,"
        "last_error=VALUES(last_error),updated_at=CURRENT_TIMESTAMP(6)",
        (ts_code, start, end, status, row_count, error),
    )


def main() -> int:
    """同步指定或全部映射基金；单只失败不阻断后续目标。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codes", help="逗号分隔六位基金代码")
    parser.add_argument("--all", action="store_true", help="同步全部映射基金")
    parser.add_argument("--resume", action="store_true", help="跳过状态为 success 的基金")
    parser.add_argument("--start", help="YYYY-MM-DD")
    parser.add_argument(
        "--end",
        default=(dt.date.today() - dt.timedelta(days=1)).isoformat(),
    )
    parser.add_argument("--page-size", type=int, default=2000)
    args = parser.parse_args()
    if not args.all and not args.codes:
        parser.error("--all 与 --codes 必须指定一个")
    if os.getenv("DB_BACKEND", "").lower() != "mysql":
        raise RuntimeError("fund_share 同步仅允许 DB_BACKEND=mysql")
    codes = None
    if args.codes:
        codes = sorted({item.strip() for item in args.codes.split(",") if item.strip()})

    connection = _connect()
    with connection.cursor() as cursor:
        targets = _load_targets(
            cursor,
            codes,
            args.resume,
            args.start,
            args.end,
        )
    reverse_map = {
        str(row["ts_code"]).upper(): str(row["fund_code"]) for row in targets
    }
    report = {
        "targets": len(targets),
        "success": 0,
        "failed": 0,
        "rows": 0,
        "errors": [],
    }
    try:
        for target in targets:
            ts_code = str(target["ts_code"]).upper()
            try:
                raw_rows = tushare_sync.fetch_share_history(
                    ts_code,
                    start_date=args.start,
                    end_date=args.end,
                    page_size=args.page_size,
                )
                rows = [
                    mapped
                    for raw in raw_rows
                    if (
                        mapped := tushare_sync.map_share_row(raw, reverse_map)
                    ) is not None
                ]
                with connection.cursor() as cursor:
                    _upsert_shares(cursor, rows)
                    _state(
                        cursor,
                        ts_code,
                        args.start,
                        args.end,
                        "success",
                        len(rows),
                    )
                connection.commit()
                report["success"] += 1
                report["rows"] += len(rows)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                connection.rollback()
                with connection.cursor() as cursor:
                    _state(
                        cursor,
                        ts_code,
                        args.start,
                        args.end,
                        "failed",
                        0,
                        str(exc)[:500],
                    )
                connection.commit()
                report["failed"] += 1
                report["errors"].append(
                    {"ts_code": ts_code, "error": type(exc).__name__}
                )
    finally:
        connection.close()
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
