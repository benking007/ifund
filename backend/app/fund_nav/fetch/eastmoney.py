"""East Money F10 historical NAV fetcher."""
from __future__ import annotations

import datetime

import requests

ENDPOINT = "https://api.fund.eastmoney.com/f10/lsjz"
REFERER = "https://fundf10.eastmoney.com/"
PAGE_SIZE = 500
FALLBACK_PAGE_SIZE = 200
REQUEST_TIMEOUT = 15


def _to_float(value):
    """Convert an optional API number to ``float``."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value or value in {"-", "--"}:
            return None
    return float(value)


def _map_row(raw_row: dict) -> dict:
    """Map one East Money row to the local NAV field names."""
    if not isinstance(raw_row, dict):
        raise ValueError("East Money LSJZList contains a non-object row")
    trade_date = raw_row["FSRQ"]
    if not trade_date:
        raise ValueError("East Money NAV row has no trade date")
    return {
        "trade_date": str(trade_date),
        "nav": _to_float(raw_row.get("DWJZ")),
        "acc_nav": _to_float(raw_row.get("LJJZ")),
        "daily_return": _to_float(raw_row.get("JZZZL")),
        "cum_return": _to_float(raw_row.get("ACTUALSYI")),
    }


def _parse_page(response: requests.Response) -> tuple[list[dict], int, bool]:
    """Validate and map one F10 response page."""
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("East Money response is not a JSON object")
    err_code = payload.get("ErrCode")
    if err_code not in (None, "", 0, "0"):
        raise RuntimeError(f"East Money F10 error {err_code}: {payload.get('ErrMsg', '')}")
    data = payload.get("Data")
    if data is None:
        try:
            total_count = int(payload["TotalCount"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("East Money response has no usable Data object") from exc
        if total_count != 0:
            raise ValueError("East Money response has no Data object")
        # Some current API gateways reject pageSize=500 with Data=null even
        # though the same request succeeds with a smaller page size.
        return [], 0, True
    if not isinstance(data, dict):
        raise ValueError("East Money response has no Data object")
    raw_rows = data.get("LSJZList")
    if raw_rows is None:
        raw_rows = []
    if not isinstance(raw_rows, list):
        raise ValueError("East Money Data.LSJZList is not a list")
    total_value = data.get("TotalCount")
    if total_value is None:
        total_value = payload.get("TotalCount")
    try:
        total_count = int(total_value)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("East Money Data.TotalCount is invalid") from exc
    return [_map_row(raw_row) for raw_row in raw_rows], total_count, False


def fetch_nav_incremental(code: str, start_date: str, end_date: str) -> list[dict]:
    """Fetch and map NAV rows in the inclusive ``start_date..end_date`` range.

    The endpoint includes both date boundaries.  The caller deliberately relies
    on the database upsert to make the shared boundary row idempotent.
    """
    rows: list[dict] = []
    page_index = 1
    page_size = PAGE_SIZE
    received_count = 0
    using_fallback_page_size = False
    headers = {"Referer": REFERER}
    while True:
        response = requests.get(
            ENDPOINT,
            params={
                "fundCode": code,
                "pageIndex": page_index,
                "pageSize": page_size,
                "startDate": start_date,
                "endDate": end_date,
            },
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        page_rows, total_count, data_missing = _parse_page(response)
        if data_missing and page_size == PAGE_SIZE:
            page_size = FALLBACK_PAGE_SIZE
            using_fallback_page_size = True
            continue
        if not page_rows:
            break
        rows.extend(page_rows)
        received_count += len(page_rows)
        if using_fallback_page_size and len(page_rows) < page_size:
            # The current gateway may cap the actual response at 20 rows even
            # when pageSize=200; use the reported total instead of page*size.
            if received_count >= total_count:
                break
            page_index += 1
            continue
        if page_index * page_size >= total_count:
            break
        page_index += 1
    return rows


def fetch_nav_full(code: str) -> list[dict]:
    """Fetch the full available history as the non-akshare fallback helper."""
    return fetch_nav_incremental(code, "2000-01-01", datetime.date.today().isoformat())
