"""按 ann_date 窗口批量同步 Tushare fund_div（低 API 调用量策略）。"""

# Event mapping mirrors the per-fund event worker by design.
# pylint: disable=duplicate-code

from __future__ import annotations

import datetime as dt
import logging
import os
import time

from app import db as database
from app.fund_nav import sync_state, ts_code_map
from app.fund_nav.crud import div_split_crud
from app.fund_nav.fetch import tushare_client

logger = logging.getLogger(__name__)

TASK_KIND = "div_ann_date"
DEFAULT_INTERVAL = max(float(os.getenv("TUSHARE_INTERVAL_MS", "3000")) / 1000.0, 2.0)
DIV_FIELDS = "ts_code,ann_date,ex_date,record_date,pay_date,div_cash,div_proc,base_date"


def _date(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text or None


def _float(value: object) -> float | None:
    try:
        return float(value) if value not in (None, "", "-") else None
    except (TypeError, ValueError):
        return None


def _ts_to_fund_code(ts_code: str, reverse: dict[str, str]) -> str | None:
    """ts_code → fund_code；优先反向映射。"""
    ts = str(ts_code or "").strip().upper()
    if ts in reverse:
        return reverse[ts]
    base = ts.split(".", 1)[0]
    return base if len(base) == 6 and base.isdigit() else None


def _rows_from_tushare(raw_rows: list[dict], reverse_map: dict[str, str]) -> list[dict]:
    out: list[dict] = []
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    for raw in raw_rows:
        ts = str(raw.get("ts_code") or "").strip().upper()
        fund_code = _ts_to_fund_code(ts, reverse_map)
        ex_date = _date(
            raw.get("ex_date") or raw.get("div_date") or raw.get("net_ex_date")
        )
        if not fund_code or not ex_date:
            continue
        cash = _float(raw.get("div_cash"))
        if cash is None:
            continue
        out.append(
            {
                "fund_code": fund_code,
                "ts_code": ts,
                "ex_date": ex_date,
                "ann_date": _date(raw.get("ann_date")),
                "record_date": _date(raw.get("record_date")),
                "pay_date": _date(raw.get("pay_date")),
                "event_type": "div",
                "cash_per_unit": cash,
                "split_ratio": None,
                "source": "tushare",
                "fetch_time": now,
            }
        )
    return out


def get_reverse_map() -> dict[str, str]:
    """加载 ts_code 到基金代码的反向映射。"""
    mapping = ts_code_map.load_map()
    return {ts: code for code, ts in mapping.items()}


def get_watermark() -> str | None:
    """全局 ann_date 水位（fund_code='__global__'）。"""
    row = database.select_one(
        sync_state.TABLE,
        {
            "fund_code": "eq.__global__",
            "task_kind": f"eq.{TASK_KIND}",
        },
    )
    if not row:
        return None
    val = row.get("watermark_date")
    if val is None:
        return None
    if isinstance(val, dt.date):
        return val.isoformat()
    return str(val)[:10]


def set_watermark(iso_date: str) -> None:
    """推进全局分红公告日水位。"""
    sync_state.set_watermark("__global__", TASK_KIND, iso_date)


def sync_ann_date(ann_yyyymmdd: str, reverse_map: dict[str, str] | None = None) -> dict:
    """同步单个公告日窗口的分红事件。"""
    rev = reverse_map if reverse_map is not None else get_reverse_map()
    raw = tushare_client.call("fund_div", {"ann_date": ann_yyyymmdd}, DIV_FIELDS)
    rows = _rows_from_tushare(raw, rev)
    written = div_split_crud.upsert_events(rows) if rows else 0
    return {"ann_date": ann_yyyymmdd, "fetched": len(raw), "written": written}


def iter_dates(start: dt.date, end: dt.date):
    """生成闭区间自然日。"""
    cur = start
    while cur <= end:
        yield cur
        cur += dt.timedelta(days=1)


def run_backfill(
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    limit_days: int | None = None,
    interval: float | None = None,
) -> dict:
    """按日 ann_date 回填；默认从水位+1 到昨天。"""
    interval = interval if interval is not None else DEFAULT_INTERVAL
    end = (
        dt.date.fromisoformat(end_date)
        if end_date
        else dt.datetime.now().astimezone().date() - dt.timedelta(days=1)
    )
    if start_date:
        start = dt.date.fromisoformat(start_date)
    else:
        wm = get_watermark()
        start = (
            dt.date.fromisoformat(wm) + dt.timedelta(days=1)
            if wm
            else dt.date(1998, 1, 1)
        )

    rev = get_reverse_map()
    days = list(iter_dates(start, end))
    if limit_days is not None:
        days = days[: max(0, int(limit_days))]

    total_fetched = total_written = errors = 0
    last_ok = None
    for day in days:
        ann = day.strftime("%Y%m%d")
        try:
            result = sync_ann_date(ann, rev)
            total_fetched += int(result.get("fetched") or 0)
            total_written += int(result.get("written") or 0)
            last_ok = day.isoformat()
            set_watermark(day.isoformat())
            if result.get("written"):
                logger.info(
                    "div ann_date=%s fetched=%s written=%s",
                    ann,
                    result["fetched"],
                    result["written"],
                )
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            errors += 1
            logger.warning("div ann_date=%s failed: %s", ann, exc)
            sync_state.mark_failed("__global__", TASK_KIND, exc)
        time.sleep(interval)

    return {
        "days_processed": len(days),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "last_watermark": last_ok,
        "fetched": total_fetched,
        "written": total_written,
        "errors": errors,
    }
