"""基金前复权净值治理引擎。

Tushare ``adj_nav`` 作为主结果；事件因子是可审计的本地兜底。前复权采用
「最新交易日为基准」口径：除权日前的历史净值乘以之后所有事件的因子，
再除以最新日因子。
"""
from __future__ import annotations

import logging
import math
from collections.abc import Iterable

from app.common.network import NetworkRetryExhausted
from app.fund_nav.crud import div_split_crud, nav_crud, repair_crud
from app.fund_nav.fetch import events_worker, tushare_client
from app.fund_nav.fetch.events_worker import call_tushare_with_retry


logger = logging.getLogger(__name__)
ADJ_TOLERANCE = 0.005


def _date(value: object) -> str | None:
    """统一事件/净值日期格式。"""
    if value is None:
        return None
    value = str(value).strip()
    if len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return value or None


def _float(value: object) -> float | None:
    """转换可选数字并过滤 NaN。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _event_multiplier(event: dict, nav_by_date: dict[str, float]) -> float:
    """计算一条分红/拆分事件的历史价格倍率。"""
    event_type = event.get("event_type")
    if event_type == "split":
        ratio = _float(event.get("split_ratio"))
        return ratio if ratio is not None and ratio > 0 else 1.0
    if event_type != "div":
        return 1.0
    cash = _float(event.get("cash_per_unit"))
    if cash is None or cash <= 0:
        return 1.0
    ex_date = _date(event.get("ex_date"))
    base_nav = nav_by_date.get(ex_date or "")
    if base_nav is None and ex_date:
        future_dates = [day for day in nav_by_date if day >= ex_date]
        if future_dates:
            base_nav = nav_by_date[min(future_dates)]
    if base_nav is None or base_nav <= 0:
        logger.warning("分红事件缺少有效除息日净值，跳过因子：%s", event)
        return 1.0
    return (base_nav + cash) / base_nav


def calculate_event_factors(nav_rows: Iterable[dict], events: Iterable[dict]) -> dict[str, float]:
    """计算每个净值日的未归一化前复权因子 ``f(t)``。"""
    valid_nav = {}
    for row in nav_rows:
        day = _date(row.get("trade_date") or row.get("nav_date"))
        raw_nav = row.get("nav")
        if raw_nav is None:
            raw_nav = row.get("unit_nav")
        nav = _float(raw_nav)
        if day and nav is not None and nav > 0:
            valid_nav[day] = nav

    event_items = []
    for event in events:
        ex_date = _date(event.get("ex_date"))
        if not ex_date:
            continue
        multiplier = _event_multiplier(event, valid_nav)
        if multiplier != 1.0:
            event_items.append((ex_date, multiplier))

    factors = {}
    for day in valid_nav:
        factor = 1.0
        for ex_date, multiplier in event_items:
            if ex_date > day:
                factor *= multiplier
        factors[day] = factor
    return factors


def calculate_adjusted_rows(nav_rows: list[dict], events: list[dict]) -> list[dict]:
    """基于事件返回带 ``adj_nav`` 的净值行，不执行数据库写入。"""
    factors = calculate_event_factors(nav_rows, events)
    latest_day = max(factors, default=None)
    latest_factor = factors.get(latest_day, 1.0)
    result = []
    for row in nav_rows:
        item = dict(row)
        day = _date(row.get("trade_date") or row.get("nav_date"))
        raw_nav = row.get("nav")
        if raw_nav is None:
            raw_nav = row.get("unit_nav")
        nav = _float(raw_nav)
        factor = factors.get(day or "")
        item["trade_date"] = day or row.get("trade_date")
        item["adj_nav"] = (
            nav * factor / latest_factor
            if nav is not None and factor is not None and latest_factor
            else None
        )
        item["adj_src"] = "calc"
        result.append(item)
    return result


def relative_error(left: float | None, right: float | None) -> float:
    """返回相对误差；零基准不相等时返回无穷大。"""
    if left is None or right is None:
        return math.inf
    denominator = abs(float(left))
    if denominator == 0:
        return 0.0 if float(right) == 0 else math.inf
    return abs(float(right) - float(left)) / denominator


def fetch_adj_tushare(
    codes: list[str],
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """按基金拉取 Tushare 全史/区间 adj_nav 并写回 fund_nav。"""
    total_rows = 0
    failed: list[str] = []
    unique_codes = list(dict.fromkeys(str(item).strip() for item in codes if str(item).strip()))
    for code in unique_codes:
        try:
            source_rows = call_tushare_with_retry(
                f"基金 {code} 前复权净值",
                tushare_client.fetch_fund_nav,
                code,
                start_date,
                end_date,
                timeout=tushare_client.REQUEST_TIMEOUT,
            )
            mapped = []
            for row in source_rows:
                day = _date(row.get("nav_date"))
                adj_nav = _float(row.get("adj_nav"))
                if day and adj_nav is not None:
                    mapped.append({
                        "trade_date": day,
                        "unit_nav": _float(row.get("unit_nav")),
                        "accum_nav": _float(row.get("accum_nav")),
                        "adj_nav": adj_nav,
                    })
            if not mapped:
                raise RuntimeError("Tushare adj_nav 返回空或无有效行")
            total_rows += nav_crud.upsert_adj_rows(code, mapped, source="tushare")
        except Exception as exc:  # pylint: disable=broad-exception-caught
            failed.append(code)
            try:
                attempts = exc.attempts if isinstance(exc, NetworkRetryExhausted) else 1
                repair_crud.record_failure(code, "adj", str(exc), attempts=attempts)
            except Exception as queue_exc:  # pylint: disable=broad-exception-caught
                logger.warning("基金 %s adj 失败入队也失败：%s", code, queue_exc)
            logger.warning("Tushare adj_nav 失败(%s)：%s", code, exc)
    return {"funds": len(unique_codes), "rows": total_rows, "failed": failed}


def calc_adj_from_events(code: str, *, only_null: bool = False) -> dict:
    """用本地分红/拆分事件计算并回刷某基金前复权净值。"""
    event_scan_status = events_worker.get_event_scan_status(code)
    if event_scan_status != "done":
        logger.warning(
            "基金 %s 事件扫描未就绪（status=%s），跳过事件自算复权",
            code,
            event_scan_status or "NULL",
        )
        return {
            "fund_code": code,
            "rows": 0,
            "nav_rows": 0,
            "events": 0,
            "only_null": only_null,
            "latest_date": None,
            "skipped": True,
            "reason": "event_scan_not_ready",
            "event_scan_status": event_scan_status,
        }
    nav_rows = nav_crud.list_nav_rows(code)
    events = div_split_crud.list_events(code)
    adjusted = calculate_adjusted_rows(nav_rows, events)
    original_by_date = {
        _date(row.get("trade_date") or row.get("nav_date")): row for row in nav_rows
    }
    rows = [
        row for row in adjusted
        if row.get("adj_nav") is not None
        and (
            not only_null
            or original_by_date.get(row.get("trade_date"), {}).get("adj_nav") is None
        )
    ]
    updated = nav_crud.update_adj_rows(code, rows, source="calc")
    return {
        "fund_code": code,
        "rows": updated,
        "nav_rows": len(nav_rows),
        "events": len(events),
        "only_null": only_null,
        "latest_date": max((row.get("trade_date") for row in nav_rows), default=None),
    }


def _sample_rows(rows: list[dict], sample_size: int) -> list[dict]:
    """均匀抽样，避免只验证最近一段。"""
    if sample_size <= 0 or not rows:
        return []
    if sample_size == 1:
        return [rows[-1]]
    if len(rows) <= sample_size:
        return rows
    step = (len(rows) - 1) / (sample_size - 1)
    return [rows[round(index * step)] for index in range(sample_size)]


def cross_check(code: str, sample_size: int = 20, tolerance: float = ADJ_TOLERANCE) -> dict:
    """抽样比较已写入的 Tushare adj_nav 与本地事件自算值。"""
    nav_rows = nav_crud.list_nav_rows(code)
    events = div_split_crud.list_events(code)
    calculated = calculate_adjusted_rows(nav_rows, events)
    calculated_by_date = {
        _date(row.get("trade_date") or row.get("nav_date")): row.get("adj_nav")
        for row in calculated
    }
    comparable = [
        row for row in nav_rows
        if row.get("adj_src") == "tushare"
        and _float(row.get("adj_nav")) is not None
        and _float(row.get("nav")) is not None
    ]
    samples = _sample_rows(comparable, sample_size)
    errors = []
    comparable_samples = []
    incomparable_samples = []
    mismatch_samples = []
    for row in samples:
        day = _date(row.get("trade_date") or row.get("nav_date"))
        tushare_adj = _float(row.get("adj_nav"))
        calculated_adj = _float(calculated_by_date.get(day))
        if tushare_adj is None or calculated_adj is None:
            sample = {
                "trade_date": day,
                "tushare_adj_nav": tushare_adj,
                "calc_adj_nav": calculated_adj,
                "reason": "calc_missing" if calculated_adj is None else "tushare_missing",
            }
            incomparable_samples.append(sample)
            logger.warning(
                "基金 %s Tushare/calc adj_nav 样本不可比：日期=%s 原因=%s",
                code, day, sample["reason"],
            )
            continue
        error = relative_error(tushare_adj, calculated_adj)
        sample = {
            "trade_date": day,
            "tushare_adj_nav": tushare_adj,
            "calc_adj_nav": calculated_adj,
            "relative_error": error,
        }
        comparable_samples.append(sample)
        errors.append(error)
        if error > tolerance:
            mismatch_samples.append(sample)
            logger.warning(
                "基金 %s Tushare/calc adj_nav 口径不一致：日期=%s 相对误差 %.4f%% 超过 %.2f%%",
                code, day, error * 100, tolerance * 100,
            )
    finite_errors = [error for error in errors if math.isfinite(error)]
    max_error = max(finite_errors, default=0.0)
    no_samples = not samples
    warnings = bool(no_samples or incomparable_samples or mismatch_samples)
    if no_samples:
        logger.warning("基金 %s adj_nav 交叉检查没有可比 Tushare 样本", code)
    if warnings:
        logger.warning(
            "基金 %s adj_nav 交叉检查告警：可比=%d 不可比=%d 口径不一致=%d",
            code, len(comparable_samples), len(incomparable_samples), len(mismatch_samples),
        )
    return {
        "fund_code": code,
        "sampled": len(samples),
        "comparable_count": len(comparable_samples),
        "incomparable_count": len(incomparable_samples),
        "mismatch_count": len(mismatch_samples),
        "comparable_samples": comparable_samples,
        "incomparable_samples": incomparable_samples,
        "mismatch_samples": mismatch_samples,
        "warning_count": len(incomparable_samples) + len(mismatch_samples) + int(no_samples),
        "no_comparable_samples": no_samples,
        "max_relative_error": max_error,
        "mean_relative_error": (
            sum(finite_errors) / len(finite_errors) if finite_errors else None
        ),
        "warning": warnings,
    }


def maintain_adj(code: str) -> dict:
    """日常净值成功后维护事件和复权缺口。"""
    event_result = events_worker.sync_one(code)
    if event_result.get("changed"):
        calc_result = calc_adj_from_events(code)
    else:
        latest_nav = nav_crud.stored_latest(code, "fund_nav")
        latest_adj = nav_crud.latest_adj_date(code)
        if latest_nav and (latest_adj is None or latest_adj < latest_nav):
            calc_result = fetch_adj_tushare([code], start_date=latest_adj, end_date=latest_nav)
        else:
            calc_result = {"rows": 0, "funds": 0, "failed": []}
    return {"events": event_result, "adj": calc_result}
