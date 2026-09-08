#!/usr/bin/env python3
"""iFund 生产 MySQL 数据自检（严格只读）。

用法：
  venv/bin/python3.12 scripts/data_selfcheck.py [--json]
  venv/bin/python3.12 scripts/data_selfcheck.py --as-of 2026-09-02 --window-days 45

数据库连接统一复用 ``app.db.get_db()``，并要求外部环境显式提供
``DB_BACKEND=mysql`` 与 ``IFUND_DB_*``。所有查询运行在 MySQL READ ONLY
事务中；脚本不会修改数据或创建表。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pymysql

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app import db as database  # pylint: disable=wrong-import-position
from app.db.mysql import MysqlDatabase  # pylint: disable=wrong-import-position

DEFAULT_WINDOW_DAYS = 45
DEFAULT_STALE_DAYS = 7


def _date_arg(value: str) -> dt.date:
    """解析 ISO 日期参数。"""
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日期必须为 YYYY-MM-DD：{value}") from exc


def _positive_int(value: str) -> int:
    """解析正整数参数。"""
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须为正整数")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="只输出一个 JSON 对象")
    parser.add_argument(
        "--as-of",
        type=_date_arg,
        default=dt.datetime.now().astimezone().date(),
        metavar="YYYY-MM-DD",
        help="自检基准日（默认：今天）",
    )
    parser.add_argument(
        "--window-days",
        type=_positive_int,
        default=DEFAULT_WINDOW_DAYS,
        help=f"净值异常近期窗口的自然日数（默认：{DEFAULT_WINDOW_DAYS}）",
    )
    parser.add_argument(
        "--stale-days",
        type=_positive_int,
        default=DEFAULT_STALE_DAYS,
        help=f"详情过期阈值的自然日数（默认：{DEFAULT_STALE_DAYS}）",
    )
    return parser


@contextmanager
def _readonly_cursor() -> Iterator[Any]:
    """从 app.db 取得 MySQL 连接，并开启服务端强制的只读事务。"""
    backend = os.getenv("DB_BACKEND", "").strip().lower()
    if backend != "mysql":
        raise RuntimeError(
            "data_selfcheck 仅允许 DB_BACKEND=mysql，拒绝回退到 legacy SQLite"
        )

    db = database.get_db()
    if not isinstance(db, MysqlDatabase):
        raise TypeError(f"app.db 返回了非 MySQL 后端：{type(db).__name__}")

    # Database 抽象暂未暴露任意只读 SQL；复用 app.db 创建和管理的连接池，
    # 并在此处显式开启 READ ONLY 事务，避免绕过生产连接配置。
    with db._connection() as connection:  # pylint: disable=protected-access
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute("START TRANSACTION")
                yield cursor
        finally:
            connection.rollback()


def _scalar(cursor: Any, sql: str, args: tuple[Any, ...] = ()) -> Any:
    """执行标量 SELECT，并返回结果行的第一列。"""
    cursor.execute(sql, args)
    row = cursor.fetchone()
    if not row:
        return None
    if isinstance(row, dict):
        return next(iter(row.values()))
    return row[0]


def _rows(cursor: Any, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    """执行多行 SELECT。"""
    cursor.execute(sql, args)
    return [dict(row) for row in (cursor.fetchall() or [])]


def _pct(part: int | None, total: int | None) -> float | None:
    return round(100.0 * part / total, 2) if part is not None and total else None


def _check_funds(cursor: Any, out: dict[str, Any], issues: list[str]) -> int:
    n_funds = int(_scalar(cursor, "SELECT COUNT(*) AS n FROM funds") or 0)
    dup_code = int(
        _scalar(
            cursor,
            "SELECT COUNT(*) AS n FROM (SELECT code FROM funds GROUP BY code HAVING COUNT(*) > 1) d",
        )
        or 0
    )
    empty_name = int(
        _scalar(
            cursor, "SELECT COUNT(*) AS n FROM funds WHERE name IS NULL OR name = ''"
        )
        or 0
    )
    empty_type = int(
        _scalar(
            cursor, "SELECT COUNT(*) AS n FROM funds WHERE type IS NULL OR type = ''"
        )
        or 0
    )
    out["funds_total"] = n_funds
    out["funds"] = {
        "total": n_funds,
        "dup_code": dup_code,
        "empty_name": empty_name,
        "empty_type": empty_type,
    }
    if dup_code:
        issues.append(f"funds 重复 code {dup_code} 组")
    if empty_name:
        issues.append(f"funds 空 name {empty_name}")
    if empty_type:
        issues.append(f"funds 空 type {empty_type}")
    out["funds_type_top"] = _rows(
        cursor,
        "SELECT type, COUNT(*) AS count FROM funds GROUP BY type ORDER BY count DESC LIMIT 8",
    )
    return n_funds


def _check_details(
    cursor: Any,
    out: dict[str, Any],
    issues: list[str],
    n_funds: int,
    stale_cutoff: dt.date,
    stale_days: int,
) -> None:
    n_det = int(_scalar(cursor, "SELECT COUNT(*) AS n FROM fund_details") or 0)
    stale = int(
        _scalar(
            cursor,
            "SELECT COUNT(*) AS n FROM fund_details WHERE fetch_time IS NULL OR fetch_time < %s",
            (stale_cutoff,),
        )
        or 0
    )
    latest_fetch = _scalar(cursor, "SELECT MAX(fetch_time) AS latest FROM fund_details")
    out["fund_details_total"] = n_det
    out["fund_details_coverage"] = _pct(n_det, n_funds)
    out["fund_details"] = {
        f"stale_{stale_days}d": stale,
        "latest_fetch": latest_fetch,
    }
    if stale and stale > n_det * 0.2:
        issues.append(f"fund_details 过期(>{stale_days}天) {stale}/{n_det}")

    nulls: dict[str, str] = {}
    for column in (
        "scale",
        "sharpe_1y",
        "sharpe_3y",
        "max_drawdown_1y",
        "max_drawdown_3y",
        "position_stock",
    ):
        null_count = int(
            _scalar(
                cursor,
                f"SELECT COUNT(*) AS n FROM fund_details WHERE `{column}` IS NULL",
            )
            or 0
        )
        nulls[column] = f"{null_count}({_pct(null_count, n_det)}%)"
    out["fund_details_nulls"] = nulls

    bad_scale = int(
        _scalar(cursor, "SELECT COUNT(*) AS n FROM fund_details WHERE scale < 0") or 0
    )
    bad_position = int(
        _scalar(
            cursor,
            "SELECT COUNT(*) AS n FROM fund_details WHERE position_stock < 0 OR position_stock > 100",
        )
        or 0
    )
    if bad_scale:
        issues.append(f"fund_details scale<0: {bad_scale}")
    if bad_position:
        issues.append(f"fund_details position_stock 越界: {bad_position}")


def _check_holdings(
    cursor: Any, out: dict[str, Any], issues: list[str], n_funds: int
) -> Any:
    n_hold = int(_scalar(cursor, "SELECT COUNT(*) AS n FROM fund_holdings") or 0)
    n_hold_funds = int(
        _scalar(cursor, "SELECT COUNT(DISTINCT fund_code) AS n FROM fund_holdings") or 0
    )
    n_bond = int(
        _scalar(
            cursor,
            "SELECT COUNT(*) AS n FROM fund_holdings WHERE holding_type = 'bond'",
        )
        or 0
    )
    out["fund_holdings"] = {
        "rows": n_hold,
        "funds": n_hold_funds,
        "coverage": _pct(n_hold_funds, n_funds),
        "bond_rows": n_bond,
    }
    out["holdings_quarters"] = _rows(
        cursor,
        "SELECT quarter, COUNT(DISTINCT fund_code) AS funds, COUNT(*) AS rows_c "
        "FROM fund_holdings GROUP BY quarter ORDER BY quarter DESC LIMIT 8",
    )

    bad_code = int(
        _scalar(
            cursor,
            "SELECT COUNT(*) AS n FROM fund_holdings "
            "WHERE holding_type = 'stock' AND (asset_code IS NULL OR CHAR_LENGTH(asset_code) != 6)",
        )
        or 0
    )
    bad_ratio = int(
        _scalar(
            cursor,
            "SELECT COUNT(*) AS n FROM fund_holdings WHERE hold_ratio < 0 OR hold_ratio > 100",
        )
        or 0
    )
    if bad_code:
        issues.append(f"fund_holdings 股票代码格式异常 {bad_code}")
    if bad_ratio:
        issues.append(f"fund_holdings hold_ratio 越界 {bad_ratio}")

    latest_quarter = _scalar(cursor, "SELECT MAX(quarter) AS latest FROM fund_holdings")
    over_sum = int(
        _scalar(
            cursor,
            "SELECT COUNT(*) AS n FROM ("
            "SELECT fund_code, SUM(hold_ratio) AS total_ratio FROM fund_holdings "
            "WHERE quarter = %s AND holding_type = 'stock' GROUP BY fund_code HAVING total_ratio > 105"
            ") h",
            (latest_quarter,),
        )
        or 0
    )
    out["fund_holdings"]["over_sum_105"] = over_sum
    if over_sum:
        issues.append(
            f"fund_holdings 最新季({latest_quarter}) 单基金占比合计>105%: {over_sum}"
        )
    return latest_quarter


def _check_nav(
    cursor: Any,
    out: dict[str, Any],
    issues: list[str],
    window_start: dt.date,
    window_days: int,
) -> None:
    n_nav = int(_scalar(cursor, "SELECT COUNT(*) AS n FROM fund_nav") or 0)
    nav_latest = _scalar(cursor, "SELECT MAX(trade_date) AS latest FROM fund_nav")
    n_cum = int(_scalar(cursor, "SELECT COUNT(*) AS n FROM fund_cum_return") or 0)
    cum_latest = _scalar(
        cursor, "SELECT MAX(trade_date) AS latest FROM fund_cum_return"
    )
    adj_nonnull = int(
        _scalar(cursor, "SELECT COUNT(*) AS n FROM fund_nav WHERE adj_nav IS NOT NULL")
        or 0
    )
    out["fund_nav"] = {
        "rows": n_nav,
        "latest": nav_latest,
        "cum_return_rows": n_cum,
        "cum_return_latest": cum_latest,
        "cum_ratio": round(n_cum / n_nav, 3) if n_nav else None,
        "adj_nonnull": adj_nonnull,
        "adj_coverage": round(adj_nonnull / n_nav * 100, 3) if n_nav else None,
    }

    bad_nav = int(
        _scalar(
            cursor,
            "SELECT COUNT(*) AS n FROM fund_nav WHERE trade_date >= %s AND (nav IS NULL OR nav <= 0)",
            (window_start,),
        )
        or 0
    )
    bad_acc = int(
        _scalar(
            cursor,
            "SELECT COUNT(*) AS n FROM fund_nav "
            "WHERE trade_date >= %s AND acc_nav IS NOT NULL AND nav IS NOT NULL AND acc_nav < nav * 0.98",
            (window_start,),
        )
        or 0
    )
    bad_return = int(
        _scalar(
            cursor,
            "SELECT COUNT(*) AS n FROM fund_nav "
            "WHERE trade_date >= %s AND (daily_return > 20 OR daily_return < -20)",
            (window_start,),
        )
        or 0
    )
    out["fund_nav_anomalies"] = {
        "window_days": window_days,
        "window_start": window_start,
        "nav_null_or_le0": bad_nav,
        "acc_lt_nav98pct": bad_acc,
        "return_gt20pct": bad_return,
    }
    if bad_nav:
        issues.append(f"fund_nav 自 {window_start} 起 nav 空或<=0: {bad_nav}")
    if bad_acc:
        issues.append(f"fund_nav 自 {window_start} 起 acc_nav < nav*0.98: {bad_acc}")
    if bad_return:
        issues.append(f"fund_nav 自 {window_start} 起日涨跌幅绝对值>20%: {bad_return}")


def _check_trade_dates(cursor: Any, out: dict[str, Any]) -> None:
    out["trade_dates"] = {
        "rows": int(_scalar(cursor, "SELECT COUNT(*) AS n FROM trade_dates") or 0),
        "min": _scalar(cursor, "SELECT MIN(trade_date) AS earliest FROM trade_dates"),
        "max": _scalar(cursor, "SELECT MAX(trade_date) AS latest FROM trade_dates"),
        "dup": int(
            _scalar(
                cursor,
                "SELECT COUNT(*) AS n FROM ("
                "SELECT trade_date FROM trade_dates GROUP BY trade_date HAVING COUNT(*) > 1"
                ") d",
            )
            or 0
        ),
    }


def _check_industry(
    cursor: Any, out: dict[str, Any], issues: list[str], latest_quarter: Any
) -> None:
    out["stock_industry_rows"] = int(
        _scalar(cursor, "SELECT COUNT(*) AS n FROM stock_industry") or 0
    )
    if not latest_quarter:
        return
    missing = int(
        _scalar(
            cursor,
            "SELECT COUNT(DISTINCT h.asset_code) AS n FROM fund_holdings h "
            "LEFT JOIN stock_industry si ON si.stock_code = h.asset_code "
            "WHERE h.quarter = %s AND h.holding_type = 'stock' AND si.stock_code IS NULL",
            (latest_quarter,),
        )
        or 0
    )
    held = int(
        _scalar(
            cursor,
            "SELECT COUNT(DISTINCT asset_code) AS n FROM fund_holdings "
            "WHERE quarter = %s AND holding_type = 'stock'",
            (latest_quarter,),
        )
        or 0
    )
    out["industry_uncovered_held"] = {
        "missing": missing,
        "held_assets": held,
        "pct": _pct(missing, held),
    }
    if held and missing / held > 0.15:
        issues.append(f"stock_industry 持仓未覆盖率 {_pct(missing, held)}% 偏高")


def _optional_count(cursor: Any, table: str) -> int | None:
    """统计可选表；仅将“表不存在”视为 None，其他数据库错误继续抛出。"""
    try:
        return int(_scalar(cursor, f"SELECT COUNT(*) AS n FROM `{table}`") or 0)
    except pymysql.err.ProgrammingError as exc:
        if exc.args and exc.args[0] == 1146:
            return None
        raise


def _check_auxiliary(cursor: Any, out: dict[str, Any], issues: list[str]) -> None:
    for table in (
        "fetch_tasks",
        "query_presets",
        "fund_snapshots",
        "fund_ai_analysis",
        "auth_tokens",
        "users",
        "fund_div_split",
        "nav_repair_queue",
        "fund_ts_code_alias",
    ):
        out[f"table_{table}"] = _optional_count(cursor, table)
    for table in ("portfolios", "holdings_computed", "transactions", "txns"):
        out[f"table_{table}"] = _optional_count(cursor, table)

    running_tasks = int(
        _scalar(
            cursor, "SELECT COUNT(*) AS n FROM fetch_tasks WHERE status = 'running'"
        )
        or 0
    )
    if running_tasks:
        issues.append(f"fetch_tasks 残留 running {running_tasks}")
    out["fetch_tasks_latest"] = _scalar(
        cursor, "SELECT MAX(updated_at) AS latest FROM fetch_tasks"
    )


def run_checks(as_of: dt.date, window_days: int, stale_days: int) -> dict[str, Any]:
    """在生产 MySQL 上执行全套只读检查。"""
    started = time.monotonic()
    window_start = as_of - dt.timedelta(days=window_days)
    stale_cutoff = as_of - dt.timedelta(days=stale_days)
    issues: list[str] = []
    out: dict[str, Any] = {
        "ok": True,
        "database_backend": "mysql",
        "as_of": as_of,
        "window_start": window_start,
        "window_days": window_days,
        "stale_cutoff": stale_cutoff,
        "stale_days": stale_days,
    }

    with _readonly_cursor() as cursor:
        n_funds = _check_funds(cursor, out, issues)
        _check_details(cursor, out, issues, n_funds, stale_cutoff, stale_days)
        latest_quarter = _check_holdings(cursor, out, issues, n_funds)
        _check_nav(cursor, out, issues, window_start, window_days)
        _check_trade_dates(cursor, out)
        _check_industry(cursor, out, issues, latest_quarter)
        _check_auxiliary(cursor, out, issues)
        n_ai = int(_scalar(cursor, "SELECT COUNT(*) AS n FROM fund_ai_analysis") or 0)
        out["fund_ai_analysis_coverage"] = _pct(n_ai, n_funds)

    out["issues"] = issues
    out["ok"] = not issues
    out["elapsed_sec"] = round(time.monotonic() - started, 1)
    return out


def _emit_human(report: dict[str, Any]) -> None:
    """输出便于人工阅读的报告。"""
    for key, value in report.items():
        if key != "issues":
            print(f"{key}: {value}")
    issues = report.get("issues") or []
    print(f"\n== 问题 {len(issues)} 项 ==")
    for issue in issues:
        print(" -", issue)


def main(argv: list[str] | None = None) -> int:
    """命令行入口；数据问题返回 1，执行错误返回 2。"""
    args = build_parser().parse_args(argv)
    try:
        report = run_checks(args.as_of, args.window_days, args.stale_days)
    # CLI 必须把连接/查询异常也封装成唯一 JSON 对象，因此在最外层统一兜底。
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        error_report = {
            "ok": False,
            "database_backend": os.getenv("DB_BACKEND", ""),
            "as_of": args.as_of,
            "issues": ["自检执行失败"],
            "error": f"{type(exc).__name__}: {exc}",
        }
        if args.json:
            print(json.dumps(error_report, ensure_ascii=False, default=str, indent=2))
        else:
            print(f"自检执行失败：{error_report['error']}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, default=str, indent=2))
    else:
        _emit_human(report)
    return 1 if report.get("issues") else 0


if __name__ == "__main__":
    raise SystemExit(main())
