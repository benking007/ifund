#!/usr/bin/env python3
"""补拉指定日期缺失的基金历史净值。

本脚本刻意独立于 ``daily_nav_sync.py``：先用两次集合查询筛出目标日期
缺失的基金，再按基金从东财 F10 拉取一个窄日期窗口。网络重试、超时、
无净值黑名单和 AkShare 全量回退均复用现有净值 worker。
"""

from __future__ import annotations

# 复用 worker 的私有重试/回退骨架是本脚本的设计约束。
# pylint: disable=protected-access,too-many-return-statements,wrong-import-position
import argparse
import datetime as dt
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
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
from app.fund_nav.crud import nav_crud
from app.fund_nav.fetch import eastmoney
from app.fund_nav.fetch import worker as nav_worker
from app.fund_nav.fetch.errors import NoNavDataError, is_no_nav_data_error, reason_for

DEFAULT_WORKERS = 8
MAX_WORKERS = 64
MAX_FAILURES = 20
WINDOW_DAYS = 5
logger = logging.getLogger("backfill_nav_date")


def _normalise_code(value: object) -> str:
    return str(value or "").strip()


def _parse_date(value: str) -> str:
    """校验 CLI 日期并规范成 ISO 格式。"""
    try:
        return dt.date.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("日期必须为 YYYY-MM-DD") from exc


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是整数") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("不能小于 0")
    return parsed


def _positive_int(value: str) -> int:
    parsed = _non_negative_int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("必须大于 0")
    return parsed


def load_target_codes(target_date: str) -> tuple[list[str], list[str]]:
    """用固定两次查询返回全量基金代码和缺少目标日期的代码。"""
    fund_rows = database.select(
        "funds",
        [("select", "code"), ("order", "code.asc")],
    )
    existing_rows = database.select(
        "fund_nav",
        [
            ("trade_date", f"eq.{target_date}"),
            ("select", "distinct fund_code"),
        ],
    )

    fund_codes = list(
        dict.fromkeys(
            code for row in fund_rows if (code := _normalise_code(row.get("code")))
        )
    )
    existing_codes = {
        code for row in existing_rows if (code := _normalise_code(row.get("fund_code")))
    }
    return fund_codes, [code for code in fund_codes if code not in existing_codes]


def _start_date(code: str, target_date: str) -> str:
    """取本地最新日期和目标日前五天中的较早者。"""
    target = dt.date.fromisoformat(target_date)
    window_start = target - dt.timedelta(days=WINDOW_DAYS)
    stored = nav_crud.stored_latest(code, "fund_nav")
    if not stored:
        return window_start.isoformat()
    stored_date = dt.date.fromisoformat(str(stored))
    return min(stored_date, window_start).isoformat()


def _as_no_nav_error(exc: BaseException) -> NoNavDataError:
    return NoNavDataError(str(exc), reason=reason_for(exc))


def _fetch_with_fallback(code: str, start_date: str, target_date: str) -> list[dict]:
    """复用 worker 的有限重试，失败后复用其 AkShare 全量解析。"""
    try:
        return nav_worker._call_with_retry(
            f"基金 {code} F10 历史净值",
            eastmoney.fetch_nav_incremental,
            code,
            start_date,
            target_date,
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        if is_no_nav_data_error(exc):
            raise _as_no_nav_error(exc) from exc
        logger.warning("F10 历史净值失败(%s)，回退 AkShare 全量：%s", code, exc)

    # _nav_rows 仅返回 stored 之后的数据；传入窗口前一天以保留 start 边界。
    cutoff = (dt.date.fromisoformat(start_date) - dt.timedelta(days=1)).isoformat()
    now = dt.datetime.now().isoformat(timespec="seconds")  # noqa: DTZ005
    try:
        return nav_worker._nav_rows(code, cutoff, now)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        if is_no_nav_data_error(exc):
            raise _as_no_nav_error(exc) from exc
        raise RuntimeError(f"基金 {code} F10 与 AkShare 均拉取失败: {exc}") from exc


def _normalise_rows(
    code: str,
    rows: list[dict],
    start_date: str,
    target_date: str,
) -> list[dict]:
    """裁剪远端结果并映射为 ``fund_nav`` upsert 行。"""
    fetch_time = dt.datetime.now().isoformat(timespec="seconds")  # noqa: DTZ005
    by_date: dict[str, dict] = {}
    for row in rows or []:
        day = str(row.get("trade_date") or "").strip()
        try:
            day = dt.date.fromisoformat(day).isoformat()
        except ValueError:
            continue
        if not start_date <= day <= target_date or row.get("nav") is None:
            continue
        by_date[day] = {
            "fund_code": code,
            "trade_date": day,
            "nav": row.get("nav"),
            "acc_nav": row.get("acc_nav"),
            "daily_return": row.get("daily_return"),
            "fetch_time": fetch_time,
        }
    return [by_date[day] for day in sorted(by_date)]


def _failure(code: str, stage: str, exc: BaseException) -> dict:
    return {
        "fund_code": code,
        "status": "failed",
        "fetched": 0,
        "backfilled": 0,
        "failure": {"fund_code": code, "stage": stage, "error": str(exc)},
    }


def _process_target(
    code: str,
    target_date: str,
    *,
    dry_run: bool,
    blacklisted_codes: set[str],
) -> dict:
    if code in blacklisted_codes:
        return {
            "fund_code": code,
            "status": "no_nav",
            "fetched": 0,
            "backfilled": 0,
        }

    try:
        start_date = _start_date(code, target_date)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return _failure(code, "stored_latest", exc)

    try:
        source_rows = _fetch_with_fallback(code, start_date, target_date)
    except NoNavDataError as exc:
        return {
            "fund_code": code,
            "status": "no_nav",
            "fetched": 0,
            "backfilled": 0,
        }
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return _failure(code, "fetch", exc)

    try:
        nav_rows = _normalise_rows(code, source_rows, start_date, target_date)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return _failure(code, "parse", exc)
    fetched = int(bool(nav_rows))
    has_target_date = any(row["trade_date"] == target_date for row in nav_rows)
    if not dry_run and nav_rows:
        try:
            nav_crud.insert_rows("fund_nav", nav_rows)
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            result = _failure(code, "write", exc)
            result["fetched"] = fetched
            return result

    return {
        "fund_code": code,
        "status": "success" if has_target_date else "no_nav",
        "fetched": fetched,
        "backfilled": int(has_target_date and not dry_run),
    }


def _empty_stats(*, dry_run: bool) -> dict:
    return {
        "total": 0,
        "target": 0,
        "fetched": 0,
        "backfilled": 0,
        "skipped": 0,
        "no_nav": 0,
        "failed": 0,
        "dry_run": dry_run,
        "failures": [],
    }


def run_backfill(
    target_date: str,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    workers: int = DEFAULT_WORKERS,
) -> dict:
    """并发补拉目标日期，返回 JSON 可序列化统计。"""
    result = _empty_stats(dry_run=dry_run)
    try:
        target_date = dt.date.fromisoformat(target_date).isoformat()
        fund_codes, target_codes = load_target_codes(target_date)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        result["failed"] = 1
        result["failures"] = [{"stage": "targets", "error": str(exc)}]
        return result

    result["total"] = len(fund_codes)
    result["target"] = len(target_codes)
    result["skipped"] = len(fund_codes) - len(target_codes)
    selected_codes = target_codes[:limit] if limit is not None else target_codes
    if not selected_codes:
        return result

    try:
        blacklisted_codes = nav_worker.no_nav_blacklist.blacklisted_codes()
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        result["failed"] = 1
        result["failures"] = [{"stage": "blacklist", "error": str(exc)}]
        return result

    max_workers = min(MAX_WORKERS, max(1, int(workers)))
    with ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="nav-date-backfill",
    ) as executor:
        futures = {
            executor.submit(
                _process_target,
                code,
                target_date,
                dry_run=dry_run,
                blacklisted_codes=blacklisted_codes,
            ): code
            for code in selected_codes
        }
        for future in as_completed(futures):
            code = futures[future]
            try:
                item = future.result()
            except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                item = _failure(code, "worker", exc)
            result["fetched"] += int(item["fetched"])
            result["backfilled"] += int(item["backfilled"])
            if item["status"] == "no_nav":
                result["no_nav"] += 1
            elif item["status"] == "failed":
                result["failed"] += 1
                if len(result["failures"]) < MAX_FAILURES:
                    result["failures"].append(item["failure"])
    return result


def build_parser() -> argparse.ArgumentParser:
    """构造独立脚本的命令行参数。"""
    parser = argparse.ArgumentParser(description="补拉指定日期缺失的基金历史净值")
    parser.add_argument(
        "--date", required=True, type=_parse_date, help="目标日期 YYYY-MM-DD"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="执行筛选和抓取，但不写数据库或黑名单"
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON 统计")
    parser.add_argument(
        "--limit", type=_non_negative_int, help="最多处理前 N 只缺失基金"
    )
    parser.add_argument(
        "--workers",
        type=_positive_int,
        default=DEFAULT_WORKERS,
        help="并发线程数（默认 8）",
    )
    return parser


def _print_human(result: dict) -> None:
    print(
        f"基金={result['total']} 缺失={result['target']} 抓到={result['fetched']} "
        f"补齐={result['backfilled']} 跳过={result['skipped']} 无净值={result['no_nav']} "
        f"失败={result['failed']} dry_run={result['dry_run']}"
    )
    for failure in result["failures"]:
        print(
            f"  {failure.get('fund_code', '-')} "
            f"[{failure.get('stage', '-')}] {failure.get('error', '')}"
        )


def main(argv: list[str] | None = None) -> int:
    """运行补拉并按输出模式返回退出码。"""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    result = run_backfill(
        args.date,
        dry_run=args.dry_run,
        limit=args.limit,
        workers=args.workers,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        _print_human(result)
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
