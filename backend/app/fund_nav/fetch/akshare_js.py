"""Direct parser for East Money's ``pingzhongdata`` JavaScript payload."""
from __future__ import annotations

import datetime
import json
import re

import requests

ENDPOINT_TEMPLATE = "https://fund.eastmoney.com/pingzhongdata/{code}.js"
REFERER = "https://fund.eastmoney.com/"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)
REQUEST_TIMEOUT = 15
SHANGHAI_TZ = datetime.timezone(datetime.timedelta(hours=8))


def _to_float(value):
    """Convert an optional JavaScript number/string to ``float``."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip().rstrip("%")
        if not value or value in {"-", "--", "null", "None"}:
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _trade_date(timestamp) -> str | None:
    """Convert a JavaScript millisecond timestamp to a Shanghai date."""
    milliseconds = _to_float(timestamp)
    if milliseconds is None:
        return None
    try:
        return datetime.datetime.fromtimestamp(
            milliseconds / 1000,
            tz=datetime.timezone.utc,
        ).astimezone(SHANGHAI_TZ).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _extract_array(javascript: str, variable: str) -> list | None:
    """Extract one JSON array assignment without evaluating JavaScript."""
    pattern = rf"(?:var\s+)?{re.escape(variable)}\s*=\s*(\[.*?\]);"
    match = re.search(pattern, javascript, re.DOTALL)
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []
    return payload if isinstance(payload, list) else []


def _trend_item(item) -> tuple[str, float, float | None, float | None] | None:
    """Normalize one ``Data_netWorthTrend`` item."""
    if isinstance(item, dict):
        timestamp = item.get("x")
        nav = item.get("y")
        daily_return = item.get("equityReturn")
        acc_nav = None
    elif isinstance(item, (list, tuple)) and len(item) >= 2:
        timestamp = item[0]
        nav = item[1]
        daily_return = item[2] if len(item) >= 3 else None
        # Older payloads may put cumulative NAV in the fourth position.  The
        # current object layout uses the fourth value for unitMoney instead,
        # so only accept it when it is actually numeric.
        acc_nav = _to_float(item[3]) if len(item) >= 4 else None
    else:
        return None

    trade_date = _trade_date(timestamp)
    nav_value = _to_float(nav)
    if trade_date is None or nav_value is None:
        return None
    return trade_date, nav_value, _to_float(daily_return), acc_nav


def _acc_nav_map(items: list | None) -> dict[str, float]:
    """Normalize ``Data_ACWorthTrend`` into a date-keyed map."""
    result = {}
    for item in items or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        trade_date = _trade_date(item[0])
        acc_nav = _to_float(item[1])
        if trade_date is not None and acc_nav is not None:
            result[trade_date] = acc_nav
    return result


def _parse_nav_trend(items: list | None, acc_nav_items: list | None) -> list[dict]:
    """Parse ordinary-fund unit NAV and align cumulative NAV by date."""
    acc_nav_map = _acc_nav_map(acc_nav_items)
    rows = []
    for item in items or []:
        parsed = _trend_item(item)
        if parsed is None:
            continue
        trade_date, nav, daily_return, item_acc_nav = parsed
        rows.append({
            "trade_date": trade_date,
            "nav": nav,
            "acc_nav": acc_nav_map.get(trade_date, item_acc_nav),
            "daily_return": daily_return,
        })
    return rows


def _parse_money_fund_nav(items: list | None) -> list[dict]:
    """Parse money funds' ``Data_millionCopiesIncome`` as their unit NAV.

    East Money omits ``Data_netWorthTrend`` for these funds.  In that layout
    ``Data_millionCopiesIncome`` carries the same historical unit-NAV values
    exposed by the F10 ``DWJZ`` field; cumulative NAV and daily percentage
    return are not present in this JavaScript payload.
    """
    rows = []
    for item in items or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        trade_date = _trade_date(item[0])
        nav = _to_float(item[1])
        if trade_date is None or nav is None:
            continue
        rows.append({
            "trade_date": trade_date,
            "nav": nav,
            "acc_nav": None,
            "daily_return": None,
        })
    return rows


def fetch_nav_js(code: str) -> list[dict]:
    """Fetch and parse full NAV history without executing JavaScript.

    The returned rows contain ``trade_date``, ``nav``, ``acc_nav`` and
    ``daily_return``.  Network, HTTP, malformed-payload and no-data cases all
    return an empty list so callers can preserve their existing fail contract.
    """
    try:
        response = requests.get(
            ENDPOINT_TEMPLATE.format(code=code),
            headers={"Referer": REFERER, "User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        javascript = response.text
        nav_items = _extract_array(javascript, "Data_netWorthTrend")
        if nav_items is not None:
            return _parse_nav_trend(
                nav_items,
                _extract_array(javascript, "Data_ACWorthTrend"),
            )
        return _parse_money_fund_nav(
            _extract_array(javascript, "Data_millionCopiesIncome"),
        )
    except (requests.RequestException, TypeError, ValueError, OSError):
        return []
