"""净值/累计收益率数据访问（增量）。"""
from __future__ import annotations

import datetime

from app import db as database


NAV_COLUMNS = (
    "fund_code", "trade_date", "nav", "acc_nav", "daily_return",
    "adj_nav", "adj_src", "fetch_time",
)
_VALUE_COLUMNS = tuple(column for column in NAV_COLUMNS if column != "fetch_time")


def latest_trade_date() -> str:
    """≤今天的最近交易日；无则今天。

    交易日历预填整年（含未来交易日），不能直接取最大日期，否则会拿到
    年底这类未来日期，污染 fund_details.trade_date 并使「按交易日过期」判据失效。
    """
    today = datetime.date.today().isoformat()
    row = database.select_one(
        "trade_dates", {"trade_date": f"lte.{today}", "order": "trade_date.desc"})
    return row["trade_date"] if row else today


def stored_latest(code: str, table: str):
    """某基金在指定表中已存的最新 trade_date。"""
    row = database.select_one(table, {"fund_code": f"eq.{code}", "order": "trade_date.desc"})
    return row["trade_date"] if row else None


def insert_rows(table: str, rows: list[dict]) -> None:
    """批量插入增量行（空则跳过）。"""
    if rows:
        if table == "fund_nav":
            upsert_nav_rows(rows)
        else:
            database.batch_insert(table, rows)


def _stored_rows(code: str) -> dict[str, dict]:
    """按基金读取已有净值，供写入时保留复权列。"""
    rows = database.select("fund_nav", [
        ("fund_code", f"eq.{code}"),
        ("select", ",".join(NAV_COLUMNS)),
    ])
    return {row["trade_date"]: row for row in rows if row.get("trade_date")}


def upsert_nav_rows(rows: list[dict]) -> int:
    """按基金事务幂等写入单位/累计净值，并保留已有复权值。

    东财全量和日常增量都可能再次返回边界行。统一在这里合并完整列，避免
    ``INSERT OR REPLACE`` 因为输入缺少 adj_nav 而把已治理的复权值清空。
    每只基金单独事务，避免大批量回补持有长事务。
    """
    if not rows:
        return 0
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        code = str(row.get("fund_code") or "").strip()
        day = str(row.get("trade_date") or "").strip()
        if code and day:
            grouped.setdefault(code, []).append(row)

    written = 0
    for code, code_rows in grouped.items():
        with database.get_db().transaction():
            existing = _stored_rows(code)
            payload = []
            for row in code_rows:
                day = str(row["trade_date"])
                old = existing.get(day, {})
                item = {column: row.get(column) for column in NAV_COLUMNS}
                for column in ("adj_nav", "adj_src"):
                    if item[column] is None and old.get(column) is not None:
                        item[column] = old[column]
                if item["fetch_time"] is None:
                    item["fetch_time"] = old.get("fetch_time")
                if old and all(item[column] == old.get(column) for column in _VALUE_COLUMNS):
                    continue
                payload.append(item)
            if payload:
                database.batch_insert("fund_nav", payload)
                written += len(payload)
    return written


def upsert_adj_rows(code: str, rows: list[dict], source: str = "tushare") -> int:
    """只更新某基金的复权列，单位净值缺口才使用 Tushare unit_nav 补齐。"""
    if not rows:
        return 0
    with database.get_db().transaction():
        existing = _stored_rows(code)
        now = datetime.datetime.now().isoformat()
        payload = []
        for row in rows:
            day = str(row.get("trade_date") or row.get("nav_date") or "").strip()
            adj_nav = row.get("adj_nav")
            if not day or adj_nav is None:
                continue
            old = existing.get(day, {})
            item = {
                "fund_code": code,
                "trade_date": day,
                "nav": old.get("nav") if old.get("nav") is not None else row.get("unit_nav"),
                "acc_nav": old.get("acc_nav") if old.get("acc_nav") is not None else row.get("accum_nav"),
                "daily_return": old.get("daily_return"),
                "adj_nav": adj_nav,
                "adj_src": source,
                "fetch_time": old.get("fetch_time") or now,
            }
            if old and all(item[column] == old.get(column) for column in _VALUE_COLUMNS):
                continue
            payload.append(item)
        if not payload:
            return 0
        database.batch_insert("fund_nav", payload)
        return len(payload)


def update_adj_rows(code: str, rows: list[dict], source: str = "calc") -> int:
    """写回自算/指定来源的 adj_nav，保留同日其余净值字段。"""
    return upsert_adj_rows(
        code,
        [{"trade_date": row.get("trade_date"), "adj_nav": row.get("adj_nav")}
         for row in rows],
        source=source,
    )


def list_nav_rows(code: str, *, start_date: str | None = None,
                  end_date: str | None = None) -> list[dict]:
    """读取单只基金的净值治理行；筛选下沉到基金/日期索引。"""
    params: list[tuple[str, str]] = [
        ("fund_code", f"eq.{code}"),
        ("select", ",".join(NAV_COLUMNS)),
        ("order", "trade_date.asc"),
    ]
    if start_date:
        params.append(("trade_date", f"gte.{start_date}"))
    if end_date:
        params.append(("trade_date", f"lte.{end_date}"))
    return database.select("fund_nav", params)


def latest_adj_date(code: str) -> str | None:
    """取某基金已有非空复权净值的最新日期。"""
    rows = database.select("fund_nav", [
        ("fund_code", f"eq.{code}"),
        ("adj_nav", "neq.None"),
        ("order", "trade_date.desc"),
        ("select", "trade_date,adj_nav"),
    ])
    for row in rows:
        if row.get("adj_nav") is not None:
            return row.get("trade_date")
    return None


def unit_nav_on(code: str, date: str) -> tuple[str, float] | None:
    """某基金 ``date`` 当日或之前最近一个交易日的 ``(trade_date, 单位净值)``；无则 None。

    份额折算（金额 ÷ 净值）必须用**单位净值 nav**——累计净值 acc_nav 含分红会高估份额。
    用户填的交易日可能是非交易日/停牌日，故取 ``trade_date <= date`` 的最近一条。
    """
    row = database.select_one("fund_nav", [
        ("fund_code", f"eq.{code}"),
        ("trade_date", f"lte.{date}"),
        ("order", "trade_date.desc"),
    ])
    if row and row.get("nav") is not None:
        return row["trade_date"], row["nav"]
    return None


def latest_unit_nav(code: str) -> tuple[str, float] | None:
    """某基金最新一个交易日的 ``(trade_date, 单位净值)``；无则 None（合成当前市值用）。"""
    row = database.select_one("fund_nav", [
        ("fund_code", f"eq.{code}"),
        ("order", "trade_date.desc"),
    ])
    if row and row.get("nav") is not None:
        return row["trade_date"], row["nav"]
    return None


def ytd_return(code: str) -> float | None:
    """用本地净值自算「今年以来」涨幅（%）：上年末 → 最新的累计净值区间收益。

    数据源（akshare）给的 return_ytd 是采集时点的滞后快照，常与最新净值对不上；
    本地净值天天增量更新，自算可保证「最近交易日」口径、永不滞后。
    累计净值 acc_nav 含分红、区间收益更准；缺失回退单位净值 nav。无数据返回 None。
    """
    year_end_prev = f"{datetime.date.today().year - 1}-12-31"
    base = database.select_one("fund_nav", [
        ("fund_code", f"eq.{code}"),
        ("trade_date", f"lte.{year_end_prev}"),
        ("order", "trade_date.desc"),
    ])
    last = database.select_one("fund_nav", [
        ("fund_code", f"eq.{code}"),
        ("order", "trade_date.desc"),
    ])
    if not base or not last:
        return None
    base_nav = base.get("acc_nav") or base.get("nav")
    last_nav = last.get("acc_nav") or last.get("nav")
    if not base_nav or not last_nav:
        return None
    return round((last_nav / base_nav - 1) * 100, 4)


def recent_series(code: str, limit: int = 120) -> list[float]:
    """最近 limit 个交易日的累计净值序列（缺失回退单位净值），按时间升序。

    用于列表内的迷你净值走势图：取最近一段并升序，方便前端直接绘制。
    """
    return [nav for _, nav in recent_series_dated(code, limit)]


def recent_series_dated(code: str, limit: int = 120) -> list[tuple[str, float]]:
    """最近 limit 个交易日的 ``(trade_date, 累计净值)`` 列表（缺失回退单位净值），按时间升序。

    组合净值/回撤走势需要按日期对齐多只基金，故保留交易日。
    """
    rows = database.select("fund_nav", [
        ("fund_code", f"eq.{code}"),
        ("order", "trade_date.desc"),
        ("limit", limit),
    ])
    rows.reverse()  # desc 取最近 N 条后再反转为时间升序
    series: list[tuple[str, float]] = []
    for row in rows:
        value = row.get("acc_nav")
        if value is None:
            value = row.get("nav")
        if value is not None:
            series.append((row["trade_date"], value))
    return series


def recent_series_dated_with_adj(
        code: str, limit: int = 120, *, start_date: str | None = None,
        end_date: str | None = None) -> list[dict]:
    """返回兼容旧字段的走势图行，并可按交易日做包含端点的过滤。"""
    params: list[tuple[str, str | int]] = [
        ("fund_code", f"eq.{code}"),
        ("order", "trade_date.desc"),
        ("limit", limit),
        ("select", "trade_date,nav,acc_nav,adj_nav,adj_src"),
    ]
    if start_date:
        params.append(("trade_date", f"gte.{start_date}"))
    if end_date:
        params.append(("trade_date", f"lte.{end_date}"))
    rows = database.select("fund_nav", params)
    rows.reverse()
    result = []
    for row in rows:
        value = row.get("acc_nav")
        if value is None:
            value = row.get("nav")
        if value is not None:
            result.append({
                "date": row["trade_date"],
                "nav": value,
                "unit_nav": row.get("nav"),
                "adj_nav": row.get("adj_nav"),
                "adj_src": row.get("adj_src"),
            })
    return result
