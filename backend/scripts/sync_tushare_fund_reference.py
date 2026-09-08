#!/usr/bin/env python3
"""同步 Tushare fund_company + fund_basic 到 additive 参考表。"""
from __future__ import annotations

# 两个独立运维入口刻意保留相同的安全连接前置，便于单文件执行。
# pylint: disable=duplicate-code

import argparse
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


def _upsert(cursor, table: str, rows: list[dict], key_columns: set[str]) -> None:
    if not rows:
        return
    columns = list(rows[0])
    names = ",".join(f"`{column}`" for column in columns)
    values = ",".join(["%s"] * len(columns))
    updates = ",".join(
        f"`{column}`=VALUES(`{column}`)"
        for column in columns
        if column not in key_columns
    )
    sql = (
        f"INSERT INTO `{table}` ({names}) VALUES ({values}) "
        f"ON DUPLICATE KEY UPDATE {updates}"
    )
    cursor.executemany(
        sql,
        [[row.get(column) for column in columns] for row in rows],
    )


def _load_company_lookup(cursor) -> dict[str, int]:
    cursor.execute("SELECT company_id,name,short_name FROM fund_company")
    return tushare_sync.build_company_lookup(list(cursor.fetchall()))


def _load_reverse_map(cursor) -> dict[str, str]:
    cursor.execute("SELECT fund_code,ts_code FROM fund_ts_code_map")
    return {
        str(row["ts_code"]).upper(): str(row["fund_code"])
        for row in cursor.fetchall()
    }


def _validate_sample(cursor, sample_rows: list[dict]) -> list[dict]:
    codes = [row["fund_code"] for row in sample_rows]
    placeholders = ",".join(["%s"] * len(codes))
    cursor.execute(
        "SELECT fund_code,ts_code,market,status,fund_type,invest_type,"
        "management,management_fee,company_id FROM fund_basic_ext "
        f"WHERE fund_code IN ({placeholders}) ORDER BY fund_code",
        codes,
    )
    stored = {row["fund_code"]: row for row in cursor.fetchall()}
    for expected in sample_rows:
        actual = stored.get(expected["fund_code"])
        if actual is None:
            raise RuntimeError(f"sample missing after upsert: {expected['fund_code']}")
        for field in ("ts_code", "market", "status", "fund_type", "invest_type"):
            if actual.get(field) != expected.get(field):
                raise RuntimeError(
                    f"sample mismatch {expected['fund_code']}.{field}: "
                    f"{actual.get(field)!r} != {expected.get(field)!r}"
                )
    return list(stored.values())


def main() -> int:
    """先写并核验 20 只样本，通过后在同一事务补齐全部映射基金。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page-size", type=int, default=5000)
    args = parser.parse_args()
    if os.getenv("DB_BACKEND", "").lower() != "mysql":
        raise RuntimeError("参考数据同步仅允许 DB_BACKEND=mysql")

    raw_companies = tushare_sync.fetch_companies()
    raw_basic = tushare_sync.fetch_all_basic(page_size=args.page_size)
    company_rows = [
        tushare_sync.map_company_row(row)
        for row in raw_companies
        if str(row.get("name") or "").strip()
    ]

    connection = _connect()
    try:
        with connection.cursor() as cursor:
            _upsert(cursor, "fund_company", company_rows, {"name"})
            company_lookup = _load_company_lookup(cursor)
            reverse_map = _load_reverse_map(cursor)
            basic_rows = [
                mapped
                for raw in raw_basic
                if (
                    mapped := tushare_sync.map_basic_row(
                        raw, reverse_map, company_lookup
                    )
                ) is not None
            ]
            raw_by_ts = {
                str(row.get("ts_code") or "").upper(): row for row in raw_basic
            }
            sample_raw = tushare_sync.select_basic_samples(
                [raw_by_ts[row["ts_code"]] for row in basic_rows]
            )
            sample_codes = {
                reverse_map[str(row.get("ts_code") or "").upper()]
                for row in sample_raw
            }
            sample_rows = [
                row for row in basic_rows if row["fund_code"] in sample_codes
            ]
            _upsert(cursor, "fund_basic_ext", sample_rows, {"fund_code"})
            samples = _validate_sample(cursor, sample_rows)
            _upsert(
                cursor,
                "fund_basic_ext",
                [row for row in basic_rows if row["fund_code"] not in sample_codes],
                {"fund_code"},
            )
            cursor.execute("SELECT COUNT(*) AS n FROM fund_company")
            company_count = int(cursor.fetchone()["n"])
            cursor.execute("SELECT COUNT(*) AS n FROM fund_basic_ext")
            basic_count = int(cursor.fetchone()["n"])
            cursor.execute(
                "SELECT COUNT(*) AS n FROM fund_basic_ext WHERE company_id IS NOT NULL"
            )
            associated = int(cursor.fetchone()["n"])
            mapped_bases = set(reverse_map.values())
            tushare_bases = {
                str(row.get("ts_code") or "").split(".", 1)[0]
                for row in raw_basic
            }
            cursor.execute(
                "SELECT code FROM fund_ts_code_quarantine "
                "WHERE entity_kind='ifund_unmatched'"
            )
            unmatched = {str(row["code"]) for row in cursor.fetchall()}
            newly_resolvable = sorted((unmatched & tushare_bases) - mapped_bases)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    print(json.dumps({
        "raw_company": len(raw_companies),
        "company_rows": company_count,
        "raw_basic": len(raw_basic),
        "basic_ext_rows": basic_count,
        "company_associated": associated,
        "sample_count": len(samples),
        "samples": samples,
        "quarantine_newly_resolvable": newly_resolvable,
    }, default=str, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
