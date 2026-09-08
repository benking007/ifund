#!/usr/bin/env python3
"""按净值日窗口回补 Tushare 公募基金净值，仅插入缺失唯一键。

脚本直接使用 ``INSERT IGNORE``，确保 ``(fund_code, trade_date)`` 已存在时
整行保持不变。处理状态和 quarantine 只写本地 JSON 证据文件，不修改
``fund_sync_state``、``nav_repair_queue`` 或其他生产表。
"""

from __future__ import annotations

# 运维脚本需要同时保留细粒度统计、证据和数据库事务边界。
# pylint: disable=too-many-lines,too-many-locals,too-many-statements
import argparse
import datetime as dt
import json
import math
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymysql

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)

from app.fund_nav.fetch import (  # pylint: disable=wrong-import-position,import-error
    tushare_client,
)

DEFAULT_START = "2026-08-31"
DEFAULT_PAGE_SIZE = 5000
DEFAULT_MAX_API_CALLS = 50
FIELDS = "ts_code,ann_date,nav_date,unit_nav,accum_nav,adj_nav"
TS_CODE_RE = re.compile(r"^(\d{6})\.(OF|SH|SZ)$", re.IGNORECASE)
PRIORITY_SAMPLE_CODES = ("000001", "001505", "026650", "160216", "510300")


def _now() -> str:
    """返回带本地时区的秒级时间戳。"""
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _today() -> str:
    """返回本地时区今天的 ISO 日期。"""
    return dt.datetime.now().astimezone().date().isoformat()


def _date(value: str) -> str:
    """校验并规范 CLI ISO 日期。"""
    try:
        return dt.date.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("日期必须为 YYYY-MM-DD") from exc


def _positive_int(value: str) -> int:
    """校验 CLI 正整数。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("必须是正整数") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("必须是正整数")
    return parsed


def _atomic_json(path: Path, payload: dict) -> None:
    """原子落盘 JSON 证据，避免中断留下半文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _load_json(path: Path, default: dict) -> dict:
    """读取 JSON object，不存在时返回调用方默认值。"""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取状态文件 {path}: {type(exc).__name__}") from exc
    if not isinstance(loaded, dict):
        raise TypeError(f"状态文件 {path} 不是 JSON object")
    return loaded


def connect_mysql():
    """使用环境变量连接 MySQL，不回显凭据。"""
    if os.getenv("DB_BACKEND", "").lower() != "mysql":
        raise RuntimeError("仅允许在 DB_BACKEND=mysql 时运行")
    required = ("IFUND_DB_HOST", "IFUND_DB_USER", "IFUND_DB_PASSWORD", "IFUND_DB_NAME")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError("缺少生产数据库环境变量: " + ",".join(missing))
    return pymysql.connect(
        host=os.environ["IFUND_DB_HOST"],
        port=int(os.environ.get("IFUND_DB_PORT", "3306")),
        user=os.environ["IFUND_DB_USER"],
        password=os.environ["IFUND_DB_PASSWORD"],
        database=os.environ["IFUND_DB_NAME"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
        connect_timeout=10,
        read_timeout=60,
        write_timeout=60,
    )


@dataclass
class ApiBudget:
    """限制本进程的 Tushare 逻辑请求次数。"""

    maximum: int
    used: int = 0

    def reserve(self) -> None:
        """预留一次请求，超出预算时在调用上游前失败。"""
        if self.used >= self.maximum:
            raise RuntimeError(f"Tushare 请求预算耗尽（上限 {self.maximum} 次）")
        self.used += 1


def fetch_date_pages(
    nav_date: str, page_size: int, budget: ApiBudget
) -> tuple[list[dict], int]:
    """按 offset 拉完单日全市场；检测服务端忽略 offset 的重复页。"""
    compact_date = nav_date.replace("-", "")
    rows: list[dict] = []
    seen_signatures: set[tuple[Any, ...]] = set()
    page_count = 0
    offset = 0
    while True:
        budget.reserve()
        page = tushare_client.call(
            "fund_nav",
            {"nav_date": compact_date, "limit": page_size, "offset": offset},
            FIELDS,
        )
        page_count += 1
        if page:
            signature = (
                len(page),
                page[0].get("ts_code"),
                page[0].get("nav_date"),
                page[-1].get("ts_code"),
                page[-1].get("nav_date"),
            )
            if signature in seen_signatures:
                raise RuntimeError(f"Tushare 疑似忽略 offset，重复页 offset={offset}")
            seen_signatures.add(signature)
        rows.extend(page)
        if len(page) < page_size:
            break
        offset += len(page)
    return rows, page_count


def load_trade_dates(cursor, start: str, end: str) -> list[str]:
    """从生产交易日历选择闭区间日期，并禁止越过今天。"""
    today = _today()
    effective_end = min(end, today)
    cursor.execute(
        "SELECT trade_date FROM trade_dates "
        "WHERE trade_date BETWEEN %s AND %s ORDER BY trade_date",
        (start, effective_end),
    )
    return [str(row["trade_date"]) for row in cursor.fetchall()]


def load_mapping(cursor) -> tuple[dict[str, str], dict[str, str]]:
    """加载唯一 ts_code↔六位基金代码映射。"""
    cursor.execute("SELECT fund_code,ts_code FROM fund_ts_code_map ORDER BY fund_code")
    reverse: dict[str, str] = {}
    forward: dict[str, str] = {}
    for row in cursor.fetchall():
        fund_code = str(row.get("fund_code") or "").strip()
        ts_code = str(row.get("ts_code") or "").strip().upper()
        if fund_code and ts_code:
            reverse[ts_code] = fund_code
            forward[fund_code] = ts_code
    return reverse, forward


def count_date(cursor, nav_date: str) -> int:
    """使用日期索引统计某日事实行数。"""
    cursor.execute(
        "SELECT COUNT(*) AS n FROM fund_nav FORCE INDEX (ix_fund_nav_trade_date) "
        "WHERE trade_date=%s",
        (nav_date,),
    )
    return int(cursor.fetchone()["n"])


def existing_codes(cursor, nav_date: str) -> set[str]:
    """使用日期索引加载某日已有唯一键的基金代码部分。"""
    cursor.execute(
        "SELECT fund_code FROM fund_nav FORCE INDEX (ix_fund_nav_trade_date) "
        "WHERE trade_date=%s",
        (nav_date,),
    )
    return {str(row["fund_code"]) for row in cursor.fetchall()}


def _number(value: object) -> float | None:
    """把有限数字规范成 float；空值或非法值返回 None。"""
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def map_source_rows(
    nav_date: str,
    source_rows: list[dict],
    reverse_map: dict[str, str],
    all_mapped_ts_codes: set[str],
) -> tuple[list[dict], dict[str, list[dict] | list[str]]]:
    """映射并校验单日源数据；异常项只进入本地 quarantine。"""
    compact_date = nav_date.replace("-", "")
    fetch_time = _now()
    by_key: dict[tuple[str, str], dict] = {}
    source_ts_codes: set[str] = set()
    quarantine: dict[str, list] = {
        "unmapped_source_codes": [],
        "mapped_code_mismatch": [],
        "invalid_source_rows": [],
        "missing_unit_nav": [],
        "duplicate_keys": [],
        "mapped_without_tushare": [],
    }

    for row in source_rows:
        ts_code = str(row.get("ts_code") or "").strip().upper()
        source_day = str(row.get("nav_date") or "").strip().replace("-", "")
        if ts_code:
            source_ts_codes.add(ts_code)
        match = TS_CODE_RE.fullmatch(ts_code)
        if not match or source_day != compact_date:
            quarantine["invalid_source_rows"].append(
                {
                    "ts_code": ts_code,
                    "nav_date": source_day,
                    "reason": "invalid_code_or_date",
                }
            )
            continue
        fund_code = reverse_map.get(ts_code)
        if not fund_code:
            quarantine["unmapped_source_codes"].append(ts_code)
            continue
        if fund_code != match.group(1):
            quarantine["mapped_code_mismatch"].append(
                {"ts_code": ts_code, "fund_code": fund_code}
            )
            continue
        unit_nav = _number(row.get("unit_nav"))
        if unit_nav is None:
            quarantine["missing_unit_nav"].append(ts_code)
            continue
        adj_nav = _number(row.get("adj_nav"))
        key = (fund_code, nav_date)
        mapped = {
            "fund_code": fund_code,
            "trade_date": nav_date,
            "nav": unit_nav,
            "acc_nav": _number(row.get("accum_nav")),
            "daily_return": None,
            "adj_nav": adj_nav,
            "adj_src": "tushare" if adj_nav is not None else None,
            "fetch_time": fetch_time,
            "source": {
                "ts_code": ts_code,
                "ann_date": row.get("ann_date"),
                "nav_date": row.get("nav_date"),
            },
        }
        if key in by_key:
            quarantine["duplicate_keys"].append(
                {"fund_code": fund_code, "trade_date": nav_date, "ts_code": ts_code}
            )
            continue
        by_key[key] = mapped

    quarantine["unmapped_source_codes"] = sorted(
        set(quarantine["unmapped_source_codes"])
    )
    quarantine["missing_unit_nav"] = sorted(set(quarantine["missing_unit_nav"]))
    quarantine["mapped_without_tushare"] = sorted(all_mapped_ts_codes - source_ts_codes)
    return [by_key[key] for key in sorted(by_key)], quarantine


def insert_missing(cursor, rows: list[dict]) -> int:
    """只插入新唯一键；重复键由 MySQL 原子忽略，绝不 UPDATE。"""
    if not rows:
        return 0
    sql = (
        "INSERT IGNORE INTO fund_nav "
        "(fund_code,trade_date,nav,acc_nav,daily_return,adj_nav,adj_src,fetch_time) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"
    )
    cursor.executemany(
        sql,
        [
            (
                row["fund_code"],
                row["trade_date"],
                row["nav"],
                row["acc_nav"],
                row["daily_return"],
                row["adj_nav"],
                row["adj_src"],
                row["fetch_time"],
            )
            for row in rows
        ],
    )
    return int(cursor.rowcount)


def _quarantine_counts(quarantine: dict[str, list]) -> dict[str, int]:
    return {key: len(value) for key, value in quarantine.items()}


def _checkpoint(
    state_path: Path,
    state: dict,
    nav_date: str,
    status: str,
    summary: dict,
) -> None:
    dates = state.setdefault("dates", {})
    previous = dates.get(nav_date, {})
    dates[nav_date] = {
        "status": status,
        "attempts": int(previous.get("attempts") or 0) + 1,
        "updated_at": _now(),
        "summary": summary,
    }
    state["version"] = 1
    state["updated_at"] = _now()
    _atomic_json(state_path, state)


def process_date(
    connection,
    nav_date: str,
    reverse_map: dict[str, str],
    page_size: int,
    budget: ApiBudget,
    *,
    dry_run: bool,
) -> tuple[dict, dict[str, list], list[dict]]:
    """拉取、过滤并事务写入一个净值日。"""
    with connection.cursor() as cursor:
        before = count_date(cursor, nav_date)
        present = existing_codes(cursor, nav_date)
    source_rows, page_count = fetch_date_pages(nav_date, page_size, budget)
    if not source_rows:
        return (
            {
                "date": nav_date,
                "status": "no_data",
                "before": before,
                "after": before,
                "pages": page_count,
                "source_rows": 0,
                "mapped_rows": 0,
                "already_existing": 0,
                "planned_missing": 0,
                "inserted": 0,
                "concurrent_ignored": 0,
                "quarantine": {},
            },
            {},
            [],
        )

    mapped, quarantine = map_source_rows(
        nav_date,
        source_rows,
        reverse_map,
        set(reverse_map),
    )
    missing = [row for row in mapped if row["fund_code"] not in present]
    existing = len(mapped) - len(missing)
    inserted = 0
    if not dry_run and missing:
        try:
            with connection.cursor() as cursor:
                inserted = insert_missing(cursor, missing)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    with connection.cursor() as cursor:
        after = count_date(cursor, nav_date)
    result = {
        "date": nav_date,
        "status": "dry_run" if dry_run else "complete",
        "before": before,
        "after": after,
        "pages": page_count,
        "source_rows": len(source_rows),
        "source_distinct_ts_codes": len(
            {str(row.get("ts_code") or "").upper() for row in source_rows}
        ),
        "mapped_rows": len(mapped),
        "already_existing": existing,
        "planned_missing": len(missing),
        "inserted": inserted,
        "concurrent_ignored": 0 if dry_run else len(missing) - inserted,
        "quarantine": _quarantine_counts(quarantine),
    }
    return result, quarantine, missing


def _sample_rows(inserted_candidates: list[dict], limit: int = 10) -> list[dict]:
    """优先选择事故调查样本，再按确定顺序补足十只。"""
    by_code: dict[str, dict] = {}
    for row in reversed(inserted_candidates):
        by_code.setdefault(row["fund_code"], row)
    selected: list[dict] = []
    for code in PRIORITY_SAMPLE_CODES:
        if code in by_code:
            selected.append(by_code.pop(code))
    for code in sorted(by_code):
        if len(selected) >= limit:
            break
        selected.append(by_code[code])
    return selected[:limit]


def _float_equal(left: object, right: object) -> bool:
    """容忍 MySQL FLOAT 单精度存储误差。"""
    if left is None or right is None:
        return left is None and right is None
    return math.isclose(float(left), float(right), rel_tol=1e-5, abs_tol=1e-5)


def verify_samples(connection, candidates: list[dict]) -> list[dict]:
    """逐键对照源期望值与数据库值；最多十只，不发起额外 API。"""
    checks = []
    for expected in _sample_rows(candidates):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT fund_code,trade_date,nav,acc_nav,adj_nav,adj_src "
                "FROM fund_nav WHERE fund_code=%s AND trade_date=%s",
                (expected["fund_code"], expected["trade_date"]),
            )
            actual = cursor.fetchone()
        matches = (
            bool(actual)
            and all(
                _float_equal(actual.get(column), expected.get(column))
                for column in ("nav", "acc_nav", "adj_nav")
            )
            and actual.get("adj_src") == expected.get("adj_src")
        )
        checks.append(
            {
                "fund_code": expected["fund_code"],
                "trade_date": expected["trade_date"],
                "source": expected.get("source"),
                "expected": {
                    key: expected.get(key)
                    for key in ("nav", "acc_nav", "adj_nav", "adj_src")
                },
                "actual": {
                    key: actual.get(key)
                    for key in ("nav", "acc_nav", "adj_nav", "adj_src")
                }
                if actual
                else None,
                "matches": matches,
            }
        )
    return checks


def candidates_from_report(report: dict) -> list[dict]:
    """从既有运行报告恢复抽样期望值，供修正验证规则后离线复核。"""
    candidates = []
    for sample in report.get("verification_samples") or []:
        expected = sample.get("expected") or {}
        candidates.append(
            {
                "fund_code": sample.get("fund_code"),
                "trade_date": sample.get("trade_date"),
                "nav": expected.get("nav"),
                "acc_nav": expected.get("acc_nav"),
                "adj_nav": expected.get("adj_nav"),
                "adj_src": expected.get("adj_src"),
                "source": sample.get("source"),
            }
        )
    return [row for row in candidates if row["fund_code"] and row["trade_date"]]


def reverify_report(input_path: Path, output_path: Path) -> dict:
    """不调用 Tushare，仅用报告中缓存的源值重新核对生产事实。"""
    original = _load_json(input_path, {})
    connection = connect_mysql()
    try:
        samples = verify_samples(connection, candidates_from_report(original))
    finally:
        connection.close()
    result = {
        "verified_at": _now(),
        "input_report": str(input_path.resolve()),
        "comparison": "math.isclose(rel_tol=1e-5, abs_tol=1e-5) for MySQL FLOAT",
        "api_calls": 0,
        "samples": samples,
        "passed": bool(samples) and all(item["matches"] for item in samples),
    }
    _atomic_json(output_path.resolve(), result)
    return result


def run(args: argparse.Namespace) -> dict:
    """执行日期窗口，逐日断点并输出报告。"""
    state_path = Path(args.state_path).resolve()
    report_path = Path(args.report_path).resolve()
    quarantine_path = Path(args.quarantine_path).resolve()
    state = _load_json(state_path, {"version": 1, "dates": {}})
    budget = ApiBudget(args.max_api_calls)
    report = {
        "run_started_at": _now(),
        "window": {"start": args.start, "end": args.end},
        "page_size": args.page_size,
        "max_api_calls": args.max_api_calls,
        "write_policy": "INSERT IGNORE missing (fund_code,trade_date); never UPDATE existing rows",
        "state_path": str(state_path),
        "quarantine_path": str(quarantine_path),
        "dry_run": args.dry_run,
        "dates": [],
        "skipped_completed_dates": [],
        "errors": [],
    }
    quarantine_report: dict[str, Any] = {
        "generated_at": _now(),
        "window": report["window"],
        "dates": {},
    }
    inserted_candidates: list[dict] = []
    connection = connect_mysql()
    try:
        with connection.cursor() as cursor:
            dates = load_trade_dates(cursor, args.start, args.end)
            reverse_map, forward_map = load_mapping(cursor)
        report["trade_dates"] = dates
        report["mapping_count"] = len(forward_map)
        for nav_date in dates:
            status = state.get("dates", {}).get(nav_date, {}).get("status")
            if status == "complete" and not args.force:
                report["skipped_completed_dates"].append(nav_date)
                continue
            calls_before = budget.used
            try:
                result, quarantine, candidates = process_date(
                    connection,
                    nav_date,
                    reverse_map,
                    args.page_size,
                    budget,
                    dry_run=args.dry_run,
                )
            except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                connection.rollback()
                result = {
                    "date": nav_date,
                    "status": "failed",
                    "api_calls": budget.used - calls_before,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                }
                report["dates"].append(result)
                report["errors"].append(result)
                if not args.dry_run:
                    _checkpoint(state_path, state, nav_date, "failed", result)
                break
            result["api_calls"] = budget.used - calls_before
            report["dates"].append(result)
            quarantine_report["dates"][nav_date] = quarantine
            priority = [
                row for row in candidates if row["fund_code"] in PRIORITY_SAMPLE_CODES
            ]
            inserted_candidates.extend(priority + candidates[:10])
            if not args.dry_run:
                checkpoint_status = (
                    "complete" if result["status"] == "complete" else result["status"]
                )
                _checkpoint(state_path, state, nav_date, checkpoint_status, result)
        report["api_calls"] = budget.used
        report["total_inserted"] = sum(
            item.get("inserted", 0) for item in report["dates"]
        )
        report["verification_samples"] = (
            verify_samples(connection, inserted_candidates) if not args.dry_run else []
        )
        report["verification_passed"] = bool(report["verification_samples"]) and all(
            item["matches"] for item in report["verification_samples"]
        )
    finally:
        connection.close()
    report["run_finished_at"] = _now()
    _atomic_json(quarantine_path, quarantine_report)
    _atomic_json(report_path, report)
    return report


def build_parser() -> argparse.ArgumentParser:
    """构建受限运维 CLI。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=_date, default=DEFAULT_START)
    parser.add_argument("--end", type=_date, default=_today())
    parser.add_argument("--page-size", type=_positive_int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument(
        "--max-api-calls", type=_positive_int, default=DEFAULT_MAX_API_CALLS
    )
    parser.add_argument("--state-path", required=True)
    parser.add_argument("--report-path", required=True)
    parser.add_argument("--quarantine-path", required=True)
    parser.add_argument("--verify-report", help="不调用 API，仅复核既有报告中的抽样值")
    parser.add_argument(
        "--force", action="store_true", help="重放已完成日期，用于幂等验证"
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    """CLI 入口。"""
    args = build_parser().parse_args()
    if args.verify_report:
        result = reverify_report(Path(args.verify_report), Path(args.report_path))
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0 if result["passed"] else 2
    if args.start > args.end:
        raise SystemExit("--start 不能晚于 --end")
    report = run(args)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 2 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
