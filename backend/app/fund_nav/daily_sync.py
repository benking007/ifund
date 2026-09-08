"""基金净值日常同步：Tushare 日期窗口主源、东财补漏、AkShare 显式退避。"""

from __future__ import annotations

# 日常同步需要保留完整的运维报告、事务边界和源健康统计。
# pylint: disable=too-many-lines,too-many-locals,too-many-statements,too-many-branches,too-many-instance-attributes
import datetime as dt
import json
import logging
import math
import os
import statistics
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pymysql

from app.common import worker_base
from app.fund_nav.fetch import eastmoney, tushare_client
from app.fund_nav.fetch.errors import NoNavDataError

PAGE_SIZE = 5000
TUSHARE_FIELDS = "ts_code,ann_date,nav_date,unit_nav,accum_nav,adj_nav"
VALIDATION_THRESHOLD = 0.001
DEFAULT_TUSHARE_BUDGET = 50
DEFAULT_EASTMONEY_CONCURRENCY = 4
DEFAULT_EASTMONEY_INTERVAL = 0.05
DEFAULT_PROBE_DAYS = 12
RECONCILE_DAYS = 5
EXPECTED_LAG_MARKERS = ("QDII", "FOF")

logger = logging.getLogger(__name__)
_eastmoney_lock = threading.Lock()
_EASTMONEY_NEXT_AT = 0.0


@dataclass
class ApiBudget:
    """限制单轮 Tushare 逻辑调用数。"""

    maximum: int = DEFAULT_TUSHARE_BUDGET
    used: int = 0

    def reserve(self) -> None:
        """预留一次调用配额。"""
        if self.used >= self.maximum:
            raise RuntimeError(f"Tushare 请求预算耗尽（上限 {self.maximum} 次）")
        self.used += 1


@dataclass
class SourceHealth:
    """一轮同步的源级健康统计。"""

    tushare_calls: int = 0
    tushare_success_dates: int = 0
    tushare_failed_dates: int = 0
    tushare_rows: int = 0
    eastmoney_attempted: int = 0
    eastmoney_success: int = 0
    eastmoney_empty: int = 0
    eastmoney_failed: int = 0
    akshare_enabled: bool = False
    akshare_attempted: int = 0
    akshare_success: int = 0
    akshare_empty: int = 0
    akshare_failed: int = 0
    failure_categories: dict[str, int] = field(default_factory=dict)

    def failure(self, category: str) -> None:
        """累计一个失败分类。"""
        self.failure_categories[category] = self.failure_categories.get(category, 0) + 1


@dataclass
class NightWindowPull:
    """单次夜间 Tushare 日期窗口拉取结果。"""

    target_date: str
    source_rows: int
    mapped_rows: int
    inserted_rows: int
    skipped_existing: int
    quarantine: dict[str, int]
    calc_adj_fallback_rows: int
    validation: dict
    forward_map: dict[str, dict]


def now_iso() -> str:
    """返回北京时间环境下的带时区时间。"""
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_json(path: Path, payload: dict) -> None:
    """原子保存运行计划/证据。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_json(path: Path) -> dict:
    """读取 JSON object。"""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{path} 不是 JSON object")
    return value


def connect_mysql():
    """用生产注入的环境变量连接 MySQL，绝不回显凭据。"""
    if os.getenv("DB_BACKEND", "").lower() != "mysql":
        raise RuntimeError("daily_nav_sync 仅允许 DB_BACKEND=mysql")
    required = (
        "IFUND_DB_HOST",
        "IFUND_DB_USER",
        "IFUND_DB_PASSWORD",
        "IFUND_DB_NAME",
    )
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError("缺少生产数据库环境变量: " + ",".join(missing))
    return pymysql.connect(
        host=os.environ["IFUND_DB_HOST"],
        port=int(os.getenv("IFUND_DB_PORT", "3306")),
        user=os.environ["IFUND_DB_USER"],
        password=os.environ["IFUND_DB_PASSWORD"],
        database=os.environ["IFUND_DB_NAME"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
        connect_timeout=10,
        read_timeout=120,
        write_timeout=120,
    )


def finite_number(value: object) -> float | None:
    """只接受有限数字，过滤空值和 NaN/Inf。"""
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def iso_date(value: object) -> str:
    """把 Tushare/MySQL 日期规范为 YYYY-MM-DD。"""
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return dt.date.fromisoformat(text).isoformat()


def fetch_tushare_date(
    nav_date: str,
    budget: ApiBudget,
    *,
    page_size: int = PAGE_SIZE,
) -> list[dict]:
    """按 nav_date 全市场分页，禁止退化为逐基金调用。"""
    compact = nav_date.replace("-", "")
    rows: list[dict] = []
    seen_pages: set[tuple[Any, ...]] = set()
    offset = 0
    while True:
        budget.reserve()
        page = tushare_client.call(
            "fund_nav",
            {"nav_date": compact, "limit": page_size, "offset": offset},
            TUSHARE_FIELDS,
        )
        if page:
            signature = (
                len(page),
                page[0].get("ts_code"),
                page[0].get("nav_date"),
                page[-1].get("ts_code"),
                page[-1].get("nav_date"),
            )
            if signature in seen_pages:
                raise RuntimeError(f"Tushare 疑似忽略 offset，重复页 offset={offset}")
            seen_pages.add(signature)
        rows.extend(page)
        if len(page) < page_size:
            return rows
        offset += len(page)


def load_funds(connection) -> dict[str, dict]:
    """加载基金目录。"""
    with connection.cursor() as cursor:
        cursor.execute("SELECT code,name,type FROM funds ORDER BY code")
        rows = cursor.fetchall()
    return {
        str(row["code"]): {
            "code": str(row["code"]),
            "name": str(row.get("name") or ""),
            "type": str(row.get("type") or ""),
        }
        for row in rows
    }


def load_mapping(connection) -> tuple[dict[str, str], dict[str, dict]]:
    """返回 ts_code→fund_code 和 fund_code→映射元数据。"""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT fund_code,ts_code,tushare_status FROM fund_ts_code_map ORDER BY fund_code"
        )
        rows = cursor.fetchall()
    reverse: dict[str, str] = {}
    forward: dict[str, dict] = {}
    for row in rows:
        fund_code = str(row.get("fund_code") or "").strip()
        ts_code = str(row.get("ts_code") or "").strip().upper()
        if fund_code and ts_code:
            reverse[ts_code] = fund_code
            forward[fund_code] = {
                "ts_code": ts_code,
                "status": str(row.get("tushare_status") or ""),
            }
    return reverse, forward


def map_tushare_rows(
    nav_date: str,
    source_rows: list[dict],
    reverse_map: dict[str, str],
) -> tuple[list[dict], dict[str, int]]:
    """映射并过滤单日 Tushare 数据。"""
    expected_day = nav_date.replace("-", "")
    mapped: dict[tuple[str, str], dict] = {}
    quarantine = {
        "unmapped": 0,
        "invalid_date_or_code": 0,
        "missing_or_nan_nav": 0,
        "duplicates": 0,
    }
    for source in source_rows:
        ts_code = str(source.get("ts_code") or "").strip().upper()
        source_day = str(source.get("nav_date") or "").strip().replace("-", "")
        fund_code = reverse_map.get(ts_code)
        if not fund_code:
            quarantine["unmapped"] += 1
            continue
        if source_day != expected_day or not fund_code.isdigit():
            quarantine["invalid_date_or_code"] += 1
            continue
        nav = finite_number(source.get("unit_nav"))
        if nav is None:
            quarantine["missing_or_nan_nav"] += 1
            continue
        key = (fund_code, nav_date)
        if key in mapped:
            quarantine["duplicates"] += 1
            continue
        adj_nav = finite_number(source.get("adj_nav"))
        mapped[key] = {
            "fund_code": fund_code,
            "trade_date": nav_date,
            "nav": nav,
            "acc_nav": finite_number(source.get("accum_nav")),
            "daily_return": None,
            "adj_nav": adj_nav,
            "adj_src": "tushare" if adj_nav is not None else None,
            "fetch_time": now_iso(),
        }
    return [mapped[key] for key in sorted(mapped)], quarantine


def apply_latest_adj_fallback(rows: list[dict], target_date: str) -> int:
    """最新点前复权值缺失时以单位净值自算；历史点不做无事件依据的猜测。"""
    filled = 0
    for row in rows:
        if row.get("trade_date") == target_date and row.get("adj_nav") is None:
            row["adj_nav"] = row["nav"]
            row["adj_src"] = "calc"
            filled += 1
    return filled


def load_existing_rows(connection, nav_date: str) -> dict[str, dict]:
    """读取某净值日已有事实。"""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT fund_code,trade_date,nav,acc_nav,adj_nav,adj_src FROM fund_nav "
            "FORCE INDEX (ix_fund_nav_trade_date) WHERE trade_date=%s",
            (nav_date,),
        )
        rows = cursor.fetchall()
    return {str(row["fund_code"]): row for row in rows}


def validate_existing_rows(
    source_rows: list[dict],
    existing: dict[str, dict],
    *,
    threshold: float = VALIDATION_THRESHOLD,
) -> dict:
    """比较已有单位净值；差异超过 0.1% 只告警、不覆盖。"""
    checked = 0
    warning_count = 0
    warnings: list[dict] = []
    for row in source_rows:
        old = existing.get(row["fund_code"])
        if not old:
            continue
        expected = finite_number(row.get("nav"))
        actual = finite_number(old.get("nav"))
        if expected is None or actual is None:
            continue
        checked += 1
        difference = abs(expected - actual) / abs(expected) if expected else math.inf
        if difference > threshold:
            warning_count += 1
            if len(warnings) < 100:
                warnings.append(
                    {
                        "fund_code": row["fund_code"],
                        "trade_date": row["trade_date"],
                        "source_nav": expected,
                        "stored_nav": actual,
                        "relative_difference": round(difference, 8),
                    }
                )
    return {
        "checked": checked,
        "threshold": threshold,
        "warning_count": warning_count,
        "warnings": warnings,
        "write_policy": "warning_only; existing fact is never overwritten",
    }


def insert_rows_and_advance_watermarks(connection, rows: list[dict]) -> int:
    """同一事务 INSERT IGNORE 事实并推进水位。"""
    if not rows:
        return 0
    unique: dict[tuple[str, str], dict] = {}
    for row in rows:
        code = str(row.get("fund_code") or "").strip()
        day = iso_date(row.get("trade_date"))
        nav = finite_number(row.get("nav"))
        if code and nav is not None:
            unique[(code, day)] = {
                **row,
                "fund_code": code,
                "trade_date": day,
                "nav": nav,
            }
    payload = [unique[key] for key in sorted(unique)]
    if not payload:
        return 0
    fact_sql = (
        "INSERT IGNORE INTO fund_nav "
        "(fund_code,trade_date,nav,acc_nav,daily_return,adj_nav,adj_src,fetch_time) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"
    )
    state_sql = (
        "INSERT INTO fund_sync_state "
        "(fund_code,task_kind,watermark_date,status,attempts,last_error) "
        "VALUES (%s,'nav',%s,'success',0,NULL) "
        "ON DUPLICATE KEY UPDATE "
        "watermark_date=GREATEST(COALESCE(watermark_date,'1000-01-01'),VALUES(watermark_date)),"
        "status='success',attempts=0,last_error=NULL"
    )
    try:
        with connection.cursor() as cursor:
            cursor.executemany(
                fact_sql,
                [
                    (
                        row["fund_code"],
                        row["trade_date"],
                        row["nav"],
                        finite_number(row.get("acc_nav")),
                        finite_number(row.get("daily_return")),
                        finite_number(row.get("adj_nav")),
                        row.get("adj_src"),
                        row.get("fetch_time") or now_iso(),
                    )
                    for row in payload
                ],
            )
            inserted = int(cursor.rowcount)
            cursor.executemany(
                state_sql,
                [(row["fund_code"], row["trade_date"]) for row in payload],
            )
        connection.commit()
        return inserted
    except BaseException:
        connection.rollback()
        raise


def expected_gap_reason(fund: dict, mapping: dict | None) -> str | None:
    """分类预期缺口；这些基金不硬补、不进入 repair。"""
    value = f"{fund.get('type', '')} {fund.get('name', '')}".upper()
    if "QDII" in value:
        return "expected_qdii_disclosure_lag"
    if "FOF" in value:
        return "expected_fof_disclosure_lag"
    if mapping and mapping.get("status") == "D":
        return "expected_tushare_delisted"
    return None


def classify_missing(
    funds: dict[str, dict],
    mapping: dict[str, dict],
    present_codes: set[str],
) -> tuple[list[str], dict[str, str]]:
    """把目标日缺口拆成东财补漏集合与预期缺口。"""
    fallback: list[str] = []
    expected: dict[str, str] = {}
    for code, fund in funds.items():
        if code in present_codes:
            continue
        reason = expected_gap_reason(fund, mapping.get(code))
        if reason:
            expected[code] = reason
        else:
            fallback.append(code)
    return fallback, expected


def _wait_eastmoney_slot() -> None:
    """跨线程限制东财补漏节奏。"""
    global _EASTMONEY_NEXT_AT  # pylint: disable=global-statement
    try:
        interval = max(
            0.0,
            float(
                os.getenv("IFUND_EASTMONEY_INTERVAL", str(DEFAULT_EASTMONEY_INTERVAL))
            ),
        )
    except ValueError:
        interval = DEFAULT_EASTMONEY_INTERVAL
    with _eastmoney_lock:
        current = time.monotonic()
        wait = max(0.0, _EASTMONEY_NEXT_AT - current)
        if wait:
            time.sleep(wait)
        _EASTMONEY_NEXT_AT = max(current, _EASTMONEY_NEXT_AT) + interval


def _eastmoney_one(code: str, start_date: str, end_date: str) -> dict:
    """获取单基金东财窗口；不写 blacklist/repair。"""
    try:
        _wait_eastmoney_slot()
        source_rows = eastmoney.fetch_nav_incremental(code, start_date, end_date)
        rows = []
        for source in source_rows:
            try:
                day = iso_date(source.get("trade_date"))
            except (TypeError, ValueError):
                continue
            nav = finite_number(source.get("nav"))
            if nav is None or not start_date <= day <= end_date:
                continue
            rows.append(
                {
                    "fund_code": code,
                    "trade_date": day,
                    "nav": nav,
                    "acc_nav": finite_number(source.get("acc_nav")),
                    "daily_return": finite_number(source.get("daily_return")),
                    "adj_nav": None,
                    "adj_src": None,
                    "fetch_time": now_iso(),
                }
            )
        apply_latest_adj_fallback(rows, end_date)
        return {"code": code, "status": "success" if rows else "empty", "rows": rows}
    except NoNavDataError as exc:
        return {"code": code, "status": "empty", "rows": [], "category": exc.reason}
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return {
            "code": code,
            "status": "failed",
            "rows": [],
            "category": type(exc).__name__,
            "error": str(exc)[:500],
        }


def fetch_eastmoney_missing(
    codes: list[str],
    start_date: str,
    end_date: str,
    health: SourceHealth,
    *,
    concurrency: int = DEFAULT_EASTMONEY_CONCURRENCY,
) -> tuple[list[dict], list[dict]]:
    """并发补漏，返回有效行和受限失败样本。"""
    health.eastmoney_attempted += len(codes)
    rows: list[dict] = []
    failures: list[dict] = []
    if not codes:
        return rows, failures
    with ThreadPoolExecutor(max_workers=max(1, min(16, concurrency))) as executor:
        futures = {
            executor.submit(_eastmoney_one, code, start_date, end_date): code
            for code in codes
        }
        for future in as_completed(futures):
            result = future.result()
            status = result["status"]
            if status == "success":
                health.eastmoney_success += 1
                rows.extend(result["rows"])
            elif status == "empty":
                health.eastmoney_empty += 1
                health.failure(str(result.get("category") or "eastmoney_empty"))
            else:
                health.eastmoney_failed += 1
                health.failure(str(result.get("category") or "eastmoney_failed"))
                if len(failures) < 100:
                    failures.append(result)
    return rows, failures


def _akshare_one(code: str, start_date: str, end_date: str) -> dict:
    """子进程中的 AkShare 退避；只返回窗口行。"""
    try:
        import akshare as ak  # pylint: disable=import-outside-toplevel,import-error

        frame = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
        rows = []
        if frame is not None:
            for _, source in frame.iterrows():
                try:
                    day = iso_date(source.get("净值日期"))
                except (TypeError, ValueError):
                    continue
                nav = worker_base.safe_float(source.get("单位净值"))
                if finite_number(nav) is None or not start_date <= day <= end_date:
                    continue
                rows.append(
                    {
                        "fund_code": code,
                        "trade_date": day,
                        "nav": nav,
                        "acc_nav": None,
                        "daily_return": worker_base.safe_float(source.get("日增长率")),
                        "adj_nav": None,
                        "adj_src": None,
                        "fetch_time": now_iso(),
                    }
                )
        apply_latest_adj_fallback(rows, end_date)
        return {"code": code, "status": "success" if rows else "empty", "rows": rows}
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return {
            "code": code,
            "status": "failed",
            "rows": [],
            "category": type(exc).__name__,
            "error": str(exc)[:500],
        }


def fetch_akshare_missing(
    codes: list[str],
    start_date: str,
    end_date: str,
    health: SourceHealth,
    *,
    enabled: bool,
) -> tuple[list[dict], list[dict]]:
    """显式开关启用 AkShare 子进程退避。"""
    health.akshare_enabled = enabled
    if not enabled or not codes:
        return [], []
    health.akshare_attempted += len(codes)
    rows: list[dict] = []
    failures: list[dict] = []
    concurrency = max(1, min(4, int(os.getenv("IFUND_AKSHARE_CONCURRENCY", "2"))))
    with ProcessPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(_akshare_one, code, start_date, end_date): code
            for code in codes
        }
        for future in as_completed(futures):
            result = future.result()
            if result["status"] == "success":
                health.akshare_success += 1
                rows.extend(result["rows"])
            elif result["status"] == "empty":
                health.akshare_empty += 1
            else:
                health.akshare_failed += 1
                health.failure(str(result.get("category") or "akshare_failed"))
                if len(failures) < 100:
                    failures.append(result)
    return rows, failures


def trade_dates(connection, end_date: str, limit: int) -> list[str]:
    """读取不晚于指定日的最近交易日，按新到旧。"""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT trade_date FROM trade_dates WHERE trade_date<=%s "
            "ORDER BY trade_date DESC LIMIT %s",
            (end_date, limit),
        )
        rows = cursor.fetchall()
    return [iso_date(row["trade_date"]) for row in rows]


def is_trade_date(connection, day: str) -> bool:
    """判断本地交易日历是否包含日期。"""
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM trade_dates WHERE trade_date=%s LIMIT 1", (day,))
        return cursor.fetchone() is not None


def resolve_night_target(
    connection,
    today: str,
    budget: ApiBudget,
    health: SourceHealth,
) -> tuple[str, list[dict]]:
    """从当天向前探测首个非空净值日，披露开始后自然切回当天。"""
    candidates = [today]
    candidates.extend(
        candidate
        for candidate in trade_dates(connection, today, DEFAULT_PROBE_DAYS)
        if candidate != today
    )
    for candidate in candidates:
        calls_before = budget.used
        rows = fetch_tushare_date(candidate, budget)
        health.tushare_calls += budget.used - calls_before
        health.tushare_rows += len(rows)
        if rows:
            health.tushare_success_dates += 1
            return candidate, rows
        health.tushare_failed_dates += 1
    raise RuntimeError("未在探测窗口找到最近已披露净值日")


def _existing_codes(connection, nav_date: str) -> set[str]:
    return set(load_existing_rows(connection, nav_date))


def _remaining_codes(connection, nav_date: str, candidates: list[str]) -> list[str]:
    present = _existing_codes(connection, nav_date)
    return [code for code in candidates if code not in present]


def continuity_gap_keys(
    candidates: set[str] | list[str],
    nav_dates: list[str],
    present_by_date: dict[str, set[str]],
) -> list[tuple[str, str]]:
    """返回最近窗口内逐基金缺失的 ``(fund_code, nav_date)`` 键。"""
    return [
        (code, nav_date)
        for code in sorted(set(candidates))
        for nav_date in nav_dates
        if code not in present_by_date.get(nav_date, set())
    ]


def _remaining_gap_codes(
    connection,
    nav_dates: list[str],
    candidates: list[str],
) -> list[str]:
    """重查事实表，返回指定窗口仍至少缺一个键的基金。"""
    if not candidates:
        return []
    present_by_date = {
        nav_date: _existing_codes(connection, nav_date) for nav_date in nav_dates
    }
    return sorted(
        {
            code
            for code, _nav_date in continuity_gap_keys(
                candidates, nav_dates, present_by_date
            )
        }
    )


def _fallback_and_write(
    connection,
    codes: list[str],
    start_date: str,
    end_date: str,
    health: SourceHealth,
    *,
    eastmoney_concurrency: int,
    akshare_enabled: bool,
    required_dates: list[str] | None = None,
) -> tuple[int, list[dict]]:
    rows, failures = fetch_eastmoney_missing(
        codes,
        start_date,
        end_date,
        health,
        concurrency=eastmoney_concurrency,
    )
    inserted = insert_rows_and_advance_watermarks(connection, rows)
    remaining = _remaining_gap_codes(connection, required_dates or [end_date], codes)
    ak_rows, ak_failures = fetch_akshare_missing(
        remaining,
        start_date,
        end_date,
        health,
        enabled=akshare_enabled,
    )
    inserted += insert_rows_and_advance_watermarks(connection, ak_rows)
    return inserted, failures + ak_failures


def integrity_gate(connection, target_date: str) -> dict:
    """输出覆盖率、十工作日基线和水位滞后分布。"""
    baseline_dates = trade_dates(connection, target_date, 11)
    previous_dates = [day for day in baseline_dates if day < target_date][:10]
    count_by_date: dict[str, int] = {target_date: 0}
    if previous_dates:
        placeholders = ",".join(["%s"] * (len(previous_dates) + 1))
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT trade_date,COUNT(*) n FROM fund_nav FORCE INDEX "
                f"(ix_fund_nav_trade_date) WHERE trade_date IN ({placeholders}) GROUP BY trade_date",
                [target_date, *previous_dates],
            )
            count_by_date.update(
                {str(row["trade_date"]): int(row["n"]) for row in cursor.fetchall()}
            )
    baseline_values = [count_by_date.get(day, 0) for day in previous_dates]
    baseline = statistics.median(baseline_values) if baseline_values else 0
    target_count = count_by_date.get(target_date, 0)
    coverage = target_count / baseline if baseline else 0.0

    history = trade_dates(connection, target_date, 260)
    positions = {day: index for index, day in enumerate(history)}
    lag = {"T0": 0, "T-1": 0, "T-2~3": 0, "T-4~7": 0, ">T-7": 0}
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT watermark_date,COUNT(*) n FROM fund_sync_state "
            "WHERE task_kind='nav' GROUP BY watermark_date"
        )
        state_rows = cursor.fetchall()
        cursor.execute("SELECT COUNT(*) n FROM funds")
        total_funds = int(cursor.fetchone()["n"])
    state_count = 0
    for row in state_rows:
        count = int(row["n"])
        state_count += count
        watermark = iso_date(row["watermark_date"]) if row.get("watermark_date") else ""
        distance = positions.get(watermark, 999)
        if distance <= 0:
            lag["T0"] += count
        elif distance == 1:
            lag["T-1"] += count
        elif distance <= 3:
            lag["T-2~3"] += count
        elif distance <= 7:
            lag["T-4~7"] += count
        else:
            lag[">T-7"] += count
    lag[">T-7"] += max(0, total_funds - state_count)
    alert = bool(baseline and coverage < 0.8)
    result = {
        "target_date": target_date,
        "target_rows": target_count,
        "baseline_dates": previous_dates,
        "baseline_counts": baseline_values,
        "baseline_median": baseline,
        "coverage": round(coverage, 6),
        "coverage_alert_below_80pct": alert,
        "lag_distribution": lag,
    }
    if alert:
        logger.error(
            "完整性 gate ERROR：目标日 %s 行数=%d，近十工作日基线=%.1f，覆盖率=%.2f%% < 80%%",
            target_date,
            target_count,
            baseline,
            coverage * 100,
        )
    else:
        logger.info(
            "完整性 gate：目标日 %s 行数=%d，近十工作日基线=%.1f，覆盖率=%.2f%%，滞后=%s",
            target_date,
            target_count,
            baseline,
            coverage * 100,
            lag,
        )
    return result


def _base_report(round_name: str, health: SourceHealth) -> dict:
    return {
        "round": round_name,
        "started_at": now_iso(),
        "source_priority": [
            "tushare_nav_date_window",
            "eastmoney_lsjz",
            "akshare_opt_in",
        ],
        "adj_policy": (
            "Tushare adj_nav first; latest target point calc fallback uses adj_nav=nav; "
            "historical calc remains in adj_engine"
        ),
        "howbuy": "removed_from_daily_chain",
        "fetch_tasks_audit": "disabled; JSON evidence is authoritative for this job",
        "source_health": asdict(health),
    }


def _pull_night_window(
    connection,
    *,
    today: str,
    health: SourceHealth,
    validate_existing: bool,
    budget_maximum: int = DEFAULT_TUSHARE_BUDGET,
) -> NightWindowPull:
    """拉取一个非空日期窗口，只写入尚未落库的键。"""
    budget = ApiBudget(budget_maximum)
    target_date, source_rows = resolve_night_target(connection, today, budget, health)
    reverse_map, forward_map = load_mapping(connection)
    mapped, quarantine = map_tushare_rows(target_date, source_rows, reverse_map)
    calc_adj_fallback = apply_latest_adj_fallback(mapped, target_date)
    existing = load_existing_rows(connection, target_date)
    validation = validate_existing_rows(mapped, existing) if validate_existing else {}
    new_rows = [row for row in mapped if row["fund_code"] not in existing]
    inserted_tushare = insert_rows_and_advance_watermarks(connection, new_rows)
    return NightWindowPull(
        target_date=target_date,
        source_rows=len(source_rows),
        mapped_rows=len(mapped),
        inserted_rows=inserted_tushare,
        skipped_existing=len(mapped) - len(new_rows),
        quarantine=quarantine,
        calc_adj_fallback_rows=calc_adj_fallback,
        validation=validation,
        forward_map=forward_map,
    )


def _advance_poll_state(
    plan_path: Path,
    *,
    today: str,
    pull: NightWindowPull,
) -> dict:
    """保存独立 cron 进程间的目标日、行数和连续无变化轮数。"""
    previous: dict = {}
    if plan_path.exists():
        try:
            loaded = load_json(plan_path)
            if loaded.get("business_date") == today:
                previous = loaded
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("夜间轮询状态不可读，将重建：%s", type(exc).__name__)
    previous_target = previous.get("target_date")
    target_changed = previous_target is not None and previous_target != pull.target_date
    disclosure_count_changed = previous.get("tushare_source_rows") is not None and (
        previous.get("tushare_source_rows") != pull.source_rows
        or previous.get("tushare_mapped_rows") != pull.mapped_rows
    )
    unchanged = (
        bool(previous)
        and not target_changed
        and not disclosure_count_changed
        and pull.inserted_rows == 0
    )
    noop_streak = int(previous.get("noop_streak") or 0) + 1 if unchanged else 0
    state = {
        "version": 2,
        "business_date": today,
        "target_date": pull.target_date,
        "last_polled_at": now_iso(),
        "poll_count": int(previous.get("poll_count") or 0) + 1,
        "tushare_source_rows": pull.source_rows,
        "tushare_mapped_rows": pull.mapped_rows,
        "last_inserted_rows": pull.inserted_rows,
        "noop_streak": noop_streak,
        "policy": (
            "five-minute Tushare nav_date polling; Eastmoney and integrity gate "
            "run only in the 23:50 finalize round"
        ),
    }
    atomic_json(plan_path, state)
    return {
        "target_changed": target_changed,
        "disclosure_count_changed": disclosure_count_changed,
        "noop_streak": noop_streak,
        "poll_count": state["poll_count"],
        "state": state,
    }


def _night_report_fields(pull: NightWindowPull, state_result: dict) -> dict:
    """生成轻量轮和收尾轮共用的 Tushare 摘要。"""
    return {
        "target_date": pull.target_date,
        "tushare_source_rows": pull.source_rows,
        "tushare_mapped": pull.mapped_rows,
        "tushare_inserted": pull.inserted_rows,
        "tushare_skipped_existing": pull.skipped_existing,
        "tushare_quarantine": pull.quarantine,
        "calc_adj_fallback_rows": pull.calc_adj_fallback_rows,
        "target_changed": state_result["target_changed"],
        "disclosure_count_changed": state_result["disclosure_count_changed"],
        "noop_streak": state_result["noop_streak"],
        "poll_count": state_result["poll_count"],
    }


def run_incremental(
    connection,
    *,
    today: str,
    plan_path: Path,
    budget_maximum: int = DEFAULT_TUSHARE_BUDGET,
) -> dict:
    """20:00 至 23:45 轻量轮：仅做 Tushare 日期窗口幂等增补。"""
    health = SourceHealth()
    pull = _pull_night_window(
        connection,
        today=today,
        health=health,
        validate_existing=False,
        budget_maximum=budget_maximum,
    )
    state_result = _advance_poll_state(plan_path, today=today, pull=pull)
    report = _base_report("incremental", health)
    report["source_priority"] = ["tushare_nav_date_window"]
    report["fetch_tasks_audit"] = (
        "disabled; poll state JSON and one-line logs are authoritative for lightweight rounds"
    )
    report.update(_night_report_fields(pull, state_result))
    report.update(
        {
            "mode": "lightweight",
            "eastmoney_skipped": True,
            "integrity_gate_skipped": True,
            "plan_path": str(plan_path),
            "finished_at": now_iso(),
        }
    )
    report["source_health"] = asdict(health)
    return report


def run_finalize(
    connection,
    *,
    today: str,
    plan_path: Path,
    eastmoney_concurrency: int,
    akshare_enabled: bool,
    budget_maximum: int = DEFAULT_TUSHARE_BUDGET,
) -> dict:
    """23:50 末轮：末次 Tushare 增补后执行东财补漏和完整性报告。"""
    health = SourceHealth(akshare_enabled=akshare_enabled)
    pull = _pull_night_window(
        connection,
        today=today,
        health=health,
        validate_existing=True,
        budget_maximum=budget_maximum,
    )
    state_result = _advance_poll_state(plan_path, today=today, pull=pull)
    funds = load_funds(connection)
    forward_map = pull.forward_map
    target_date = pull.target_date
    present = _existing_codes(connection, target_date)
    fallback_codes, expected = classify_missing(funds, forward_map, present)
    inserted_fallback, failures = _fallback_and_write(
        connection,
        fallback_codes,
        target_date,
        target_date,
        health,
        eastmoney_concurrency=eastmoney_concurrency,
        akshare_enabled=akshare_enabled,
    )
    unfinished = _remaining_codes(connection, target_date, fallback_codes)
    gate = integrity_gate(connection, target_date)
    state = state_result["state"]
    state.update(
        {
            "finalized_at": now_iso(),
            "final_fallback_candidates": len(fallback_codes),
            "final_fallback_inserted": inserted_fallback,
            "final_unfinished_count": len(unfinished),
        }
    )
    atomic_json(plan_path, state)
    report = _base_report("finalize", health)
    report.update(_night_report_fields(pull, state_result))
    report.update(
        {
            "mode": "full_finalize",
            "validation": pull.validation,
            "eastmoney_candidates": len(fallback_codes),
            "fallback_inserted": inserted_fallback,
            "expected_gap_count": len(expected),
            "expected_gap_categories": _category_counts(expected.values()),
            "unfinished_count": len(unfinished),
            "unfinished_sample": unfinished[:100],
            "failures": failures,
            "plan_path": str(plan_path),
            "integrity_gate": gate,
            "finished_at": now_iso(),
        }
    )
    report["source_health"] = asdict(health)
    return report


def _category_counts(values) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[str(value)] = counts.get(str(value), 0) + 1
    return counts


def run_reconcile(
    connection,
    *,
    today: str,
    eastmoney_concurrency: int,
    akshare_enabled: bool,
    budget_maximum: int = DEFAULT_TUSHARE_BUDGET,
) -> dict:
    """08:00 对账：最近五个净值日连续性、增补、验证与水位推进。"""
    health = SourceHealth(akshare_enabled=akshare_enabled)
    report = _base_report("reconcile", health)
    budget = ApiBudget(budget_maximum)
    yesterday = (dt.date.fromisoformat(today) - dt.timedelta(days=1)).isoformat()
    candidates = trade_dates(connection, yesterday, DEFAULT_PROBE_DAYS)
    fetched: list[tuple[str, list[dict]]] = []
    for candidate in candidates:
        calls_before = budget.used
        try:
            rows = fetch_tushare_date(candidate, budget)
        except Exception:  # pylint: disable=broad-exception-caught
            health.tushare_calls += budget.used - calls_before
            health.tushare_failed_dates += 1
            raise
        health.tushare_calls += budget.used - calls_before
        health.tushare_rows += len(rows)
        if rows:
            health.tushare_success_dates += 1
            fetched.append((candidate, rows))
        else:
            health.tushare_failed_dates += 1
        if len(fetched) >= RECONCILE_DAYS:
            break
    if len(fetched) < RECONCILE_DAYS:
        raise RuntimeError(
            f"最近净值日不足 {RECONCILE_DAYS} 个，仅探测到 {len(fetched)} 个"
        )

    funds = load_funds(connection)
    reverse_map, forward_map = load_mapping(connection)
    target_date = fetched[0][0]
    date_reports = []
    validation_warning_count = 0
    tushare_missing_keys_before_insert = 0
    observed_codes: set[str] = set()
    for nav_date, source_rows in reversed(fetched):
        mapped, quarantine = map_tushare_rows(nav_date, source_rows, reverse_map)
        observed_codes.update(row["fund_code"] for row in mapped)
        calc_adj_fallback = apply_latest_adj_fallback(mapped, target_date)
        existing = load_existing_rows(connection, nav_date)
        missing_before = sum(row["fund_code"] not in existing for row in mapped)
        tushare_missing_keys_before_insert += missing_before
        validation = validate_existing_rows(mapped, existing)
        validation_warning_count += validation["warning_count"]
        inserted = insert_rows_and_advance_watermarks(connection, mapped)
        date_reports.append(
            {
                "date": nav_date,
                "source_rows": len(source_rows),
                "mapped_rows": len(mapped),
                "continuity_gaps_before": missing_before,
                "inserted_ignore": inserted,
                "quarantine": quarantine,
                "calc_adj_fallback_rows": calc_adj_fallback,
                "validation": validation,
            }
        )

    ordered_dates = [item[0] for item in reversed(fetched)]
    present_by_date = {
        nav_date: _existing_codes(connection, nav_date) for nav_date in ordered_dates
    }
    expected = {}
    continuity_candidates = set()
    for code in observed_codes:
        reason = expected_gap_reason(funds.get(code, {}), forward_map.get(code))
        if reason:
            expected[code] = reason
        else:
            continuity_candidates.add(code)
    gap_keys_before = continuity_gap_keys(
        continuity_candidates, ordered_dates, present_by_date
    )
    target_fallback, target_expected = classify_missing(
        funds, forward_map, present_by_date[target_date]
    )
    expected.update(target_expected)
    fallback_codes = sorted(
        set(target_fallback) | {code for code, _nav_date in gap_keys_before}
    )
    earliest = ordered_dates[0]
    inserted_fallback, failures = _fallback_and_write(
        connection,
        fallback_codes,
        earliest,
        target_date,
        health,
        eastmoney_concurrency=eastmoney_concurrency,
        akshare_enabled=akshare_enabled,
        required_dates=ordered_dates,
    )
    unfinished = _remaining_gap_codes(connection, ordered_dates, fallback_codes)
    present_after = {
        nav_date: _existing_codes(connection, nav_date) for nav_date in ordered_dates
    }
    gap_keys_after = continuity_gap_keys(
        continuity_candidates, ordered_dates, present_after
    )
    report.update(
        {
            "target_date": target_date,
            "reconcile_dates": ordered_dates,
            "dates": date_reports,
            "tushare_missing_keys_before_insert": tushare_missing_keys_before_insert,
            "continuity_gaps_before": len(gap_keys_before),
            "continuity_gaps_after": len(gap_keys_after),
            "continuity_gap_sample_after": gap_keys_after[:100],
            "continuity_policy": (
                "Tushare source keys are INSERT IGNORE supplemented; funds observed "
                "inside the five-day window with missing dates are sent to Eastmoney"
            ),
            "validation_warning_count": validation_warning_count,
            "eastmoney_candidates": len(fallback_codes),
            "fallback_inserted": inserted_fallback,
            "expected_gap_count": len(expected),
            "expected_gap_categories": _category_counts(expected.values()),
            "unfinished_count": len(unfinished),
            "unfinished_sample": unfinished[:100],
            "failures": failures,
            "integrity_gate": integrity_gate(connection, target_date),
            "finished_at": now_iso(),
        }
    )
    report["source_health"] = asdict(health)
    return report
