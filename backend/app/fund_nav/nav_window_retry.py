"""把 retry_nav 从单基金全史回补收成队列缺口窗口。

``nav_repair_queue`` 用 ``fund_code``、``gap_start``、``gap_end`` 定位缺口
（``fund_nav`` 的日期列是 ``trade_date``）。同一基金里相邻或重叠的窗口合并成
一次 ``fund_nav`` 请求，并带上 ``start_date``/``end_date``。开放哨兵或无法解析
的日期回退到原来的 ``backfill_one`` 全史路径。

限速不在这里另做：``tushare_client.fetch_fund_nav`` 会走共享总闸（iFund 默认
20 次/分钟）。
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from zoneinfo import ZoneInfo

from app.fund_nav.crud import nav_crud, repair_crud
from app.fund_nav.fetch import backfill_worker, tushare_client
from app.fund_nav.repair_triage import RETRY_NAV, safe_error_text


logger = logging.getLogger(__name__)

SHANGHAI = ZoneInfo("Asia/Shanghai")
# 跨周末的两个缺口仍算相邻；再远则保持各自的最小窗口。
ADJACENT_BRIDGE_DAYS = 3
MODE_WINDOW = "window"
MODE_FULL_HISTORY = "full_history"
PREVIEW_LIMIT = 20


def now_shanghai() -> dt.datetime:
    """当前上海时间（带时区）。"""
    return dt.datetime.now(SHANGHAI)


def _parse_date(value: object) -> dt.date | None:
    """把队列或 Tushare 日期收成 date；无法解析则返回 None。"""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value or "").strip()
    if not text or text.lower() in {"none", "null", "nat"}:
        return None
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        text = text[:10]
    elif len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


def _finite(value: object) -> float | None:
    """把净值收成有限浮点；空值、布尔和 NaN 丢弃。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def parse_gap_window(row: dict) -> tuple[dt.date, dt.date] | None:
    """返回可拉取的闭区间。缺日期、颠倒或开放哨兵 ``2999-12-31`` 视为无法定窗。"""
    start = _parse_date(row.get("gap_start"))
    end = _parse_date(row.get("gap_end"))
    if start is None or end is None or start > end:
        return None
    if end.isoformat() >= repair_crud.DEFAULT_GAP_END:
        return None
    return start, end


def _min_id(item: dict) -> tuple[int, str]:
    ids = [int(row["id"]) for row in item["rows"] if row.get("id") is not None]
    return (min(ids) if ids else 0, str(item["fund_code"]))


def _cluster_windows(spans: list[tuple[tuple[dt.date, dt.date], dict]]) -> list[list]:
    """把已按起点排序的窗口收成相邻簇。"""
    ordered = sorted(spans, key=lambda item: (item[0][0], item[0][1], int(item[1].get("id") or 0)))
    clusters: list[list] = []
    bridge = dt.timedelta(days=ADJACENT_BRIDGE_DAYS)
    for window, row in ordered:
        if not clusters:
            clusters.append([(window, row)])
            continue
        cluster_end = max(item[0][1] for item in clusters[-1])
        if window[0] <= cluster_end + bridge:
            clusters[-1].append((window, row))
        else:
            clusters.append([(window, row)])
    return clusters


def _items_for_fund(code: str, rows: list[dict]) -> list[dict]:
    """同一基金：相邻缺口各合成一个窗口项，无法定窗的行合成一次全史回退。"""
    windowed: list[tuple[tuple[dt.date, dt.date], dict]] = []
    missing: list[dict] = []
    for row in rows:
        window = parse_gap_window(row)
        if window is None:
            missing.append(row)
        else:
            windowed.append((window, row))
    items = []
    for cluster in _cluster_windows(windowed):
        start = min(window[0] for window, _row in cluster)
        end = max(window[1] for window, _row in cluster)
        items.append({
            "fund_code": code,
            "rows": [row for _window, row in cluster],
            "start": start,
            "end": end,
            "mode": MODE_WINDOW,
        })
    if missing:
        items.append({
            "fund_code": code,
            "rows": missing,
            "start": None,
            "end": None,
            "mode": MODE_FULL_HISTORY,
        })
    return items


def plan_nav_work(rows: list[dict]) -> list[dict]:
    """按 ``fund_code`` 规划窗口请求和全史回退，并按最小队列 id 排序。"""
    grouped: dict[str, list[dict]] = {}
    order: list[str] = []
    for row in rows:
        code = str(row.get("fund_code") or "").strip()
        if code not in grouped:
            grouped[code] = []
            order.append(code)
        grouped[code].append(row)
    items: list[dict] = []
    for code in order:
        items.extend(_items_for_fund(code, grouped[code]))
    items.sort(key=_min_id)
    return items


def _count_work(work: list[dict]) -> dict:
    window_items = [item for item in work if item["mode"] == MODE_WINDOW]
    full_items = [item for item in work if item["mode"] == MODE_FULL_HISTORY]
    return {
        "windowed_rows": sum(len(item["rows"]) for item in window_items),
        "windowed_requests": len(window_items),
        "full_history_rows": sum(len(item["rows"]) for item in full_items),
        "full_history_requests": len(full_items),
    }


def _preview(work: list[dict]) -> list[dict]:
    preview = []
    for item in work[:PREVIEW_LIMIT]:
        entry = {
            "fund_code": item["fund_code"],
            "mode": item["mode"],
            "rows": len(item["rows"]),
            "ids": [row.get("id") for row in item["rows"]],
        }
        if item["start"] is not None and item["end"] is not None:
            entry["start"] = item["start"].isoformat()
            entry["end"] = item["end"].isoformat()
        preview.append(entry)
    return preview


def plan_summary(rows: list[dict]) -> dict:
    """dry-run 用的窗口/全史计数，不访问网络、不写库。"""
    work = plan_nav_work(rows)
    summary = _count_work(work)
    summary.update({
        "stopped_rows": 0,
        "done": 0,
        "failed": 0,
        "windowed_calls": 0,
        "full_history_calls": 0,
        "preview": _preview(work),
    })
    return summary


def resolve_deadline(started: dt.datetime, stop_at: dt.time | None) -> dt.datetime | None:
    """下一次到达 ``stop_at``（Asia/Shanghai）的时刻。

    启动时若今天该时刻还没到，截止就是今天；已经过了则截止为次日。
    因此通宵任务会在次日早晨停下，而不是在 22:00 被当天的 07:50 立刻打断。
    """
    if stop_at is None:
        return None
    local = started.astimezone(SHANGHAI) if started.tzinfo else started.replace(tzinfo=SHANGHAI)
    candidate = dt.datetime.combine(local.date(), stop_at, tzinfo=SHANGHAI)
    if candidate <= local:
        candidate += dt.timedelta(days=1)
    return candidate


def should_stop(
    now: dt.datetime,
    *,
    started: dt.datetime,
    deadline: dt.datetime | None,
    max_minutes: float | None,
) -> bool:
    """到达截止时刻或运行时长上限时，不再开始下一次请求。"""
    current = now.astimezone(SHANGHAI) if now.tzinfo else now.replace(tzinfo=SHANGHAI)
    origin = started.astimezone(SHANGHAI) if started.tzinfo else started.replace(tzinfo=SHANGHAI)
    if deadline is not None and current >= deadline:
        return True
    if max_minutes is None:
        return False
    return (current - origin).total_seconds() >= float(max_minutes) * 60.0


def map_window_rows(
    code: str,
    source_rows: list[dict],
    start: dt.date,
    end: dt.date,
) -> list[dict]:
    """把 Tushare ``fund_nav`` 行收成 ``fund_nav`` 写入行，只保留窗口内单位净值。"""
    fetch_time = dt.datetime.now().isoformat(timespec="seconds")
    by_day: dict[str, dict] = {}
    for source in source_rows:
        day = _parse_date(source.get("nav_date") or source.get("trade_date"))
        nav = _finite(source.get("unit_nav"))
        if nav is None:
            nav = _finite(source.get("nav"))
        if day is None or day < start or day > end or nav is None:
            continue
        key = day.isoformat()
        if key in by_day:
            continue
        acc_nav = source.get("accum_nav")
        if acc_nav is None:
            acc_nav = source.get("acc_nav")
        adj_nav = _finite(source.get("adj_nav"))
        by_day[key] = {
            "fund_code": code,
            "trade_date": key,
            "nav": nav,
            "acc_nav": _finite(acc_nav),
            "daily_return": None,
            "adj_nav": adj_nav,
            "adj_src": "tushare" if adj_nav is not None else None,
            "fetch_time": fetch_time,
        }
    return [by_day[key] for key in sorted(by_day)]


def _row_result(row: dict, *, result: str, mode: str, error: str | None = None) -> dict:
    payload = {
        "id": row.get("id"),
        "fund_code": str(row.get("fund_code") or ""),
        "action": RETRY_NAV,
        "result": result,
        "mode": mode,
    }
    if error:
        payload["error"] = error
    return payload


def _fail_rows(rows: list[dict], error: str, mode: str) -> list[dict]:
    for row in rows:
        repair_crud.mark_failed(int(row["id"]), error)
    return [_row_result(row, result="failed", mode=mode, error=error) for row in rows]


def _done_rows(rows: list[dict], mode: str) -> list[dict]:
    for row in rows:
        repair_crud.mark_done(int(row["id"]))
    return [_row_result(row, result="done", mode=mode) for row in rows]


def _stored_dates(code: str, start: dt.date, end: dt.date) -> set[str]:
    rows = nav_crud.list_nav_rows(
        code, start_date=start.isoformat(), end_date=end.isoformat(),
    )
    found = set()
    for row in rows:
        if row.get("nav") is None:
            continue
        day = _parse_date(row.get("trade_date"))
        if day is not None:
            found.add(day.isoformat())
    return found


def _window_persisted(code: str, start: dt.date, end: dt.date, payload: list[dict]) -> bool:
    """源窗口里的每个 ``trade_date`` 都已写入 ``fund_nav.nav``。"""
    expected = {str(row["trade_date"]) for row in payload}
    if not expected:
        return False
    return expected <= _stored_dates(code, start, end)


def _execute_window(item: dict) -> list[dict]:
    code = str(item["fund_code"])
    start = item["start"]
    end = item["end"]
    rows = item["rows"]
    logger.info(
        "窗口回补 %s %s..%s 队列 %s",
        code, start.isoformat(), end.isoformat(), [row.get("id") for row in rows],
    )
    try:
        source = tushare_client.fetch_fund_nav(code, start.isoformat(), end.isoformat())
        payload = map_window_rows(code, source, start, end)
        if not payload:
            raise RuntimeError("窗口内 Tushare 无单位净值")
        nav_crud.upsert_nav_rows(payload)
        if not _window_persisted(code, start, end, payload):
            raise RuntimeError("窗口写入不完整")
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return _fail_rows(rows, safe_error_text(exc), MODE_WINDOW)
    return _done_rows(rows, MODE_WINDOW)


def _execute_full_history(item: dict) -> list[dict]:
    code = str(item["fund_code"])
    rows = item["rows"]
    logger.info("全史回退 %s 队列 %s", code, [row.get("id") for row in rows])
    try:
        outcome = backfill_worker.backfill_one(code)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return _fail_rows(rows, safe_error_text(exc), MODE_FULL_HISTORY)
    if outcome.get("status") == "success":
        return _done_rows(rows, MODE_FULL_HISTORY)
    error = safe_error_text(outcome.get("error") or outcome.get("reason") or "净值回补失败")
    return _fail_rows(rows, error, MODE_FULL_HISTORY)


def _execute_item(item: dict) -> list[dict]:
    if item["mode"] == MODE_WINDOW:
        return _execute_window(item)
    return _execute_full_history(item)


def _stopped_items(work: list[dict]) -> list[dict]:
    results = []
    for item in work:
        for row in item["rows"]:
            results.append(_row_result(row, result="stopped", mode=item["mode"]))
    return results


def execute_nav_batch(
    rows: list[dict],
    *,
    stop_at: dt.time | None = None,
    max_minutes: float | None = None,
    clock=None,
    started: dt.datetime | None = None,
) -> dict:
    """执行已选出的 retry_nav 行。到点后剩余行记为 stopped，不写库。"""
    work = plan_nav_work(rows)
    summary = _count_work(work)
    tick = clock or now_shanghai
    started_at = started if started is not None else tick()
    deadline = resolve_deadline(started_at, stop_at)
    results: list[dict] = []
    windowed_calls = 0
    full_history_calls = 0
    for index, item in enumerate(work):
        if should_stop(tick(), started=started_at, deadline=deadline, max_minutes=max_minutes):
            results.extend(_stopped_items(work[index:]))
            break
        results.extend(_execute_item(item))
        if item["mode"] == MODE_WINDOW:
            windowed_calls += 1
        else:
            full_history_calls += 1
    summary.update({
        "results": results,
        "stopped_rows": sum(1 for item in results if item["result"] == "stopped"),
        "done": sum(1 for item in results if item["result"] == "done"),
        "failed": sum(1 for item in results if item["result"] == "failed"),
        "windowed_calls": windowed_calls,
        "full_history_calls": full_history_calls,
    })
    return summary
