"""Howbuy historical NAV incremental fetcher."""

from __future__ import annotations

import datetime
from html.parser import HTMLParser

import requests

from app.common.network import HTTP_TIMEOUT
from app.fund_nav.fetch.errors import HOWBUY_EMPTY, HOWBUY_REDIRECT, NoNavDataError

ENDPOINT = "https://www.howbuy.com/fund/ajax/gmfund/history/huobi.htm"
FUND_PAGE = "https://www.howbuy.com/fund/{code}/"
PAGE_SIZE = 10
REQUEST_TIMEOUT = HTTP_TIMEOUT
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)


def _clean_text(parts: list[str]) -> str:
    """Collapse whitespace collected from one HTML table cell."""
    return " ".join("".join(parts).replace("\xa0", " ").split())


class _HistoryTableParser(HTMLParser):
    """Collect table rows and the optional ``allPage`` pagination hint."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.all_pages: int | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag == "input":
            attributes = {str(key).lower(): value for key, value in attrs}
            if attributes.get("name", "").lower() == "allpage":
                try:
                    value = int(attributes.get("value") or "")
                except ValueError:
                    return
                if value > 0:
                    self.all_pages = value
            return
        if tag == "tr":
            self._row = []
            self._cell = None
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"} and self._row is not None and self._cell is not None:
            self._row.append(_clean_text(self._cell))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
            self._cell = None


def _to_float(value: str, *, percent: bool = False) -> float | None:
    value = value.strip()
    if not value or value in {"-", "--"}:
        return None
    if percent and value.endswith("%"):
        value = value[:-1].strip()
    return float(value)


def _column_index(header: list[str], *names: str) -> int | None:
    for index, value in enumerate(header):
        if value in names:
            return index
    return None


def _parse_page(html: str) -> tuple[list[dict], int | None]:
    """Parse one normal-fund history table page."""
    parser = _HistoryTableParser()
    parser.feed(html)
    parser.close()

    for row in parser.rows:
        if "每万份收益" in row or "7日年化收益率" in row:
            raise NoNavDataError(
                "Howbuy history table has no unit NAV column",
                reason=HOWBUY_EMPTY,
            )

    header_index = None
    header = None
    for index, row in enumerate(parser.rows):
        if "净值时间" in row and "单位净值" in row:
            header_index = index
            header = row
            break
    if header is None or header_index is None:
        if not parser.rows:
            return [], parser.all_pages
        raise ValueError("Howbuy response has no recognizable NAV table header")

    date_column = _column_index(header, "净值时间")
    nav_column = _column_index(header, "单位净值")
    acc_column = _column_index(header, "累计净值")
    return_column = _column_index(header, "日涨幅", "日增长率")
    if date_column is None or nav_column is None or acc_column is None:
        raise ValueError("Howbuy NAV table is missing required columns")

    rows: list[dict] = []
    required_index = max(date_column, nav_column, acc_column)
    for cells in parser.rows[header_index + 1 :]:
        if len(cells) <= required_index:
            continue
        trade_date = cells[date_column].strip()
        try:
            datetime.date.fromisoformat(trade_date)
        except ValueError:
            continue
        daily_return = None
        if return_column is not None and len(cells) > return_column:
            daily_return = _to_float(cells[return_column], percent=True)
        rows.append(
            {
                "trade_date": trade_date,
                "nav": _to_float(cells[nav_column]),
                "acc_nav": _to_float(cells[acc_column]),
                "daily_return": daily_return,
                "cum_return": None,
            }
        )
    return rows, parser.all_pages


def fetch_nav_incremental(code: str, start_date: str, end_date: str) -> list[dict]:
    """Fetch Howbuy NAV rows in the inclusive ``start_date..end_date`` window."""
    start = datetime.date.fromisoformat(start_date)
    end = datetime.date.fromisoformat(end_date)
    if start > end:
        raise ValueError("Howbuy NAV start_date is after end_date")

    url = f"{ENDPOINT}?jjdm={code}&flag=1"
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": FUND_PAGE.format(code=code),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    result: list[dict] = []
    page = 1
    saw_history_rows = False
    previous_page_dates: tuple[str, ...] | None = None

    while True:
        response = requests.post(
            url,
            data={"page": page, "perPage": PAGE_SIZE},
            headers=headers,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=False,
        )
        status_code = getattr(response, "status_code", None)
        if status_code is not None and 300 <= status_code < 400:
            raise NoNavDataError(
                f"Howbuy history endpoint redirected with HTTP {status_code}",
                reason=HOWBUY_REDIRECT,
            )
        response.raise_for_status()
        page_rows, all_pages = _parse_page(response.text)
        if not page_rows:
            if page == 1 and not saw_history_rows:
                raise NoNavDataError(
                    "Howbuy history table contains no NAV rows",
                    reason=HOWBUY_EMPTY,
                )
            break

        saw_history_rows = True
        page_dates = tuple(row["trade_date"] for row in page_rows)
        if page_dates == previous_page_dates:
            raise ValueError("Howbuy pagination repeated the previous page")
        previous_page_dates = page_dates

        for row in page_rows:
            trade_date = datetime.date.fromisoformat(row["trade_date"])
            if start <= trade_date <= end:
                result.append(row)

        earliest = min(
            datetime.date.fromisoformat(row["trade_date"]) for row in page_rows
        )
        if earliest <= start:
            break
        if all_pages is not None and page >= all_pages:
            break
        if all_pages is None and len(page_rows) < PAGE_SIZE:
            break
        page += 1

    return result
