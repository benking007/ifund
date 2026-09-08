"""AkShare 开放基金 rank 全量快照采集与缺失净值回填。"""

from __future__ import annotations

import datetime as dt
import logging
import multiprocessing
from concurrent.futures import ProcessPoolExecutor

import akshare as ak  # pylint: disable=import-error
import akshare.fund.fund_rank_em as _fund_rank_em  # pylint: disable=import-error

from app import db as database
from app.common import worker_base
from app.common.network import HTTP_TIMEOUT, TimeoutRequestsProxy

logger = logging.getLogger(__name__)

WRITE_BATCH_SIZE = 500

_FIELD_ALIASES = {
    "fund_code": ("基金代码", "fund_code"),
    "trade_date": ("净值日期", "日期", "trade_date"),
    "nav": ("单位净值", "nav"),
    "acc_nav": ("累计净值", "acc_nav"),
    "daily_return": ("日增长率", "daily_return"),
}


class RankSnapshotParseError(ValueError):
    """rank 快照结构无法映射到 ``fund_nav``。"""


def _raw_preview(frame) -> object:
    """生成解析失败日志中的原始列名和前五行。"""
    try:
        return {
            "columns": [str(column) for column in frame.columns],
            "rows": frame.head(5).to_dict(orient="records"),
        }
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return repr(frame)


def _resolve_columns(frame) -> dict[str, str]:
    """按 AkShare 当前及兼容列名解析 ``fund_nav`` 字段映射。"""
    if frame is None or not hasattr(frame, "columns"):
        logger.error("AkShare rank 快照不是 DataFrame，原始返回=%r", frame)
        raise RankSnapshotParseError("AkShare rank 快照不是 DataFrame")
    available = {str(column): column for column in frame.columns}
    mapping = {}
    missing = []
    for target, aliases in _FIELD_ALIASES.items():
        source = next(
            (available[alias] for alias in aliases if alias in available), None
        )
        if source is None:
            missing.append("/".join(aliases))
        else:
            mapping[target] = source
    if missing:
        preview = _raw_preview(frame)
        logger.error("AkShare rank 快照缺字段 %s，原始返回=%r", missing, preview)
        raise RankSnapshotParseError(f"AkShare rank 快照缺字段: {', '.join(missing)}")
    return mapping


def _fund_code(value) -> str:
    """标准化基金代码并保留前导零。"""
    text = str(value or "").strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text.zfill(6) if text.isdigit() and len(text) <= 6 else text


def _trade_date(value) -> str | None:
    """把 ``date``/``Timestamp``/字符串统一为 ISO 日期。"""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    text = str(value).strip()
    if not text or text.lower() in {"nat", "nan", "none"}:
        return None
    try:
        return dt.date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


def parse_rank_frame(frame, *, fetch_time: str) -> tuple[list[dict], list[dict]]:
    """解析全量 rank DataFrame；坏行打印原始内容并计入失败。"""
    columns = _resolve_columns(frame)
    rows = []
    failures = []
    for index, source in frame.iterrows():
        code = _fund_code(source.get(columns["fund_code"]))
        day = _trade_date(source.get(columns["trade_date"]))
        nav = worker_base.safe_float(source.get(columns["nav"]))
        if not code or day is None or nav is None:
            raw = source.to_dict()
            logger.error("AkShare rank 行解析失败 index=%s 原始返回=%r", index, raw)
            failures.append(
                {
                    "fund_code": code or None,
                    "stage": "parse",
                    "error": "基金代码/净值日期/单位净值无效",
                }
            )
            continue
        rows.append(
            {
                "fund_code": code,
                "trade_date": day,
                "nav": nav,
                "acc_nav": worker_base.safe_float(source.get(columns["acc_nav"])),
                "daily_return": worker_base.safe_float(
                    source.get(columns["daily_return"])
                ),
                "fetch_time": fetch_time,
            }
        )
    return rows, failures


def collect_rank_snapshot() -> dict:
    """在当前（应为 worker）进程调用一次 AkShare 全量 rank 快照。"""
    fetch_time = dt.datetime.now().isoformat(timespec="seconds")  # noqa: DTZ005
    original_requests = _fund_rank_em.requests
    _fund_rank_em.requests = TimeoutRequestsProxy(
        original_requests, timeout=HTTP_TIMEOUT
    )
    try:
        frame = ak.fund_open_fund_rank_em(symbol="全部")
    finally:
        _fund_rank_em.requests = original_requests

    parsed_rows, failures = parse_rank_frame(frame, fetch_time=fetch_time)
    by_code: dict[str, dict] = {}
    for row in parsed_rows:
        old = by_code.get(row["fund_code"])
        if old is None or row["trade_date"] >= old["trade_date"]:
            by_code[row["fund_code"]] = row

    return {
        "rows": [by_code[code] for code in sorted(by_code)],
        "api_calls": 1,
        "source_rows": len(frame),
        "failed": len(failures),
        "failures": failures[:20],
        "fetch_time": fetch_time,
    }


def fetch_rank_snapshot() -> dict:
    """在独立 spawn 子进程运行 AkShare，避免主进程 socket fd 冲突。"""
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=1, mp_context=context) as executor:
        future = executor.submit(collect_rank_snapshot)
        return future.result()


def _empty_stats(*, dry_run: bool, total: int = 0) -> dict:
    return {
        "total": total,
        "snapshot_rows": 0,
        "source_rows": 0,
        "api_calls": 0,
        "snapshot_dates": [],
        "date_filtered": 0,
        "eligible": 0,
        "backfilled": 0,
        "would_backfill": 0,
        "skipped": 0,
        "no_nav": 0,
        "failed": 0,
        "rows": 0,
        "dry_run": dry_run,
        "failures": [],
        "warnings": [],
    }


def _target_codes(limit: int | None) -> list[str]:
    params: list[tuple[str, object]] = [("select", "code"), ("order", "code.asc")]
    if limit is not None:
        params.append(("limit", limit))
    return [
        code
        for row in database.select("funds", params)
        if (code := _fund_code(row.get("code")))
    ]


def _write_rows(rows: list[dict]) -> tuple[int, int, list[dict]]:
    """分批幂等 upsert；批次失败时逐行重试以给出精确失败统计。"""
    success = written = 0
    failures = []
    for start in range(0, len(rows), WRITE_BATCH_SIZE):
        chunk = rows[start : start + WRITE_BATCH_SIZE]
        try:
            database.batch_insert("fund_nav", chunk)
            success += len(chunk)
            written += len(chunk)
            continue
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            logger.warning("rank 快照批量写入失败，降级逐行定位：%s", exc)
        for row in chunk:
            try:
                database.batch_insert("fund_nav", [row])
                success += 1
                written += 1
            except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                logger.error("rank 快照写入基金 %s 失败：%s", row["fund_code"], exc)
                failures.append(
                    {
                        "fund_code": row["fund_code"],
                        "stage": "write",
                        "error": str(exc),
                    }
                )
    return success, written, failures


def run_rank_backfill(
    *,
    dry_run: bool = False,
    limit: int | None = None,
    target_date: str | None = None,
) -> dict:
    """对齐 ``funds``，仅把最新 rank 快照中晚于本地日期的行写入 ``fund_nav``。

    ``target_date`` 只校验并过滤接口实际返回的日期，不会下推为历史截止参数。
    历史日期缺口应由东财 F10 ``lsjz`` 增量通道补齐。
    """
    try:
        targets = _target_codes(limit)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        result = _empty_stats(dry_run=dry_run)
        result["failed"] = 1
        result["failures"] = [{"stage": "funds", "error": str(exc)}]
        return result

    result = _empty_stats(dry_run=dry_run, total=len(targets))
    if not targets:
        return result
    if target_date is not None:
        try:
            target_date = dt.date.fromisoformat(target_date).isoformat()
        except ValueError as exc:
            result["failed"] = 1
            result["failures"] = [{"stage": "arguments", "error": str(exc)}]
            return result
    try:
        snapshot = fetch_rank_snapshot()
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.exception("AkShare rank 快照拉取失败")
        result["failed"] = 1
        result["failures"] = [{"stage": "snapshot", "error": str(exc)}]
        return result

    snapshot_rows = snapshot.get("rows") or []
    all_snapshot_by_code = {row["fund_code"]: row for row in snapshot_rows}
    snapshot_dates = sorted(
        {row["trade_date"] for row in all_snapshot_by_code.values()}
    )
    date_filtered_codes: set[str] = set()
    if target_date is not None:
        date_filtered_codes = {
            code
            for code, row in all_snapshot_by_code.items()
            if row["trade_date"] != target_date
        }
        if date_filtered_codes:
            actual_dates = ", ".join(snapshot_dates[:10]) or "无有效日期"
            if len(snapshot_dates) > 10:
                actual_dates += ", ..."
            warning = (
                f"AkShare rank 仅返回最新净值快照；--target-date={target_date} "
                f"已过滤 {len(date_filtered_codes)} 行（实际日期：{actual_dates}）。"
                "历史日期缺口请由东财 F10 lsjz 增量通道补齐。"
            )
            logger.warning(warning)
            result["warnings"].append(warning)
    snapshot_by_code = {
        code: row
        for code, row in all_snapshot_by_code.items()
        if code not in date_filtered_codes
    }
    result.update(
        {
            "snapshot_rows": len(all_snapshot_by_code),
            "source_rows": int(snapshot.get("source_rows") or 0),
            "api_calls": int(snapshot.get("api_calls") or 0),
            "snapshot_dates": snapshot_dates,
            "date_filtered": len(date_filtered_codes),
            "failed": int(snapshot.get("failed") or 0),
            "failures": list(snapshot.get("failures") or [])[:20],
        }
    )
    parse_failed_codes = {
        failure.get("fund_code")
        for failure in result["failures"]
        if failure.get("stage") == "parse" and failure.get("fund_code")
    }

    # 一次查询拿「各快照日期已存在的基金集合」，替代逐只点查（N+1）。
    existing_by_date: dict[str, set[str]] = {}
    for day in snapshot_dates:
        try:
            existing_by_date[day] = {
                str(r.get("fund_code") or "")
                for r in database.select(
                    "fund_nav",
                    {"select": "fund_code", "trade_date": day},
                )
            }
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            result["failed"] += 1
            if len(result["failures"]) < 20:
                result["failures"].append(
                    {"stage": "existing", "error": str(exc)}
                )
            existing_by_date[day] = set()

    candidates = []
    for code in targets:
        row = snapshot_by_code.get(code)
        if row is None:
            if code in date_filtered_codes:
                result["skipped"] += 1
            elif code not in parse_failed_codes:
                result["no_nav"] += 1
            continue
        if code in existing_by_date.get(row["trade_date"], set()):
            result["skipped"] += 1
            continue
        candidates.append(row)

    result["eligible"] = len(candidates)
    if dry_run:
        result["would_backfill"] = len(candidates)
        return result

    success, written, write_failures = _write_rows(candidates)
    result["backfilled"] = success
    result["rows"] = written
    result["failed"] += len(write_failures)
    room = max(0, 20 - len(result["failures"]))
    result["failures"].extend(write_failures[:room])
    return result


def exit_code(result: dict) -> int:
    """有任一拉取、解析、查询或写入失败时返回非零。"""
    return 1 if int(result.get("failed") or 0) else 0
