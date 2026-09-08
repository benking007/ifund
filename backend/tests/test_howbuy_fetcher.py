"""Tests for the Howbuy historical NAV incremental fetcher."""

# pylint: disable=missing-function-docstring
from __future__ import annotations

from unittest import TestCase
from unittest.mock import patch

import requests

from app.fund_nav.fetch import howbuy
from app.fund_nav.fetch.errors import HOWBUY_EMPTY, HOWBUY_REDIRECT, NoNavDataError


def _history_html(rows: list[tuple[str, str, str, str]], *, all_pages: int = 1) -> str:
    body = "".join(
        "<tr>"
        f"<td>{trade_date}</td>"
        f'<td class="tdr">{nav}</td>'
        f'<td class="tdr">{acc_nav}</td>'
        f'<td class="tdr">{daily_return}</td>'
        "</tr>"
        for trade_date, nav, acc_nav, daily_return in rows
    )
    return (
        '<input type="hidden" name="allPage" '
        f'value="{all_pages}">'
        "<table><tr>"
        '<td width="25%">净值时间</td>'
        '<td width="25%">单位净值</td>'
        '<td width="25%">累计净值</td>'
        '<td width="25%">日涨幅</td>'
        f"</tr>{body}</table>"
    )


class _FakeResponse:
    """Minimal response double for ``requests.post``."""

    def __init__(self, text: str = "", status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            error = requests.HTTPError(f"HTTP {self.status_code}")
            error.response = self
            raise error


class HowbuyFetcherTests(TestCase):
    """Verify Howbuy parsing, request construction, and early pagination stop."""

    def test_maps_normal_table_spans_percent_and_missing_return(self) -> None:
        response = _FakeResponse(
            _history_html(
                [
                    (
                        "2026-09-02",
                        "8.3251",
                        "8.3251",
                        '<span class="cGreen">-1.40%</span>',
                    ),
                    (
                        "2026-09-01",
                        "8.4433",
                        "8.4433",
                        '<span class="cRed">1.25%</span>',
                    ),
                    ("2026-08-31", "8.3391", "8.3391", "--"),
                ],
                all_pages=233,
            )
        )

        with patch.object(howbuy.requests, "post", return_value=response) as post:
            rows = howbuy.fetch_nav_incremental("002910", "2026-08-31", "2026-09-02")

        self.assertEqual(
            rows,
            [
                {
                    "trade_date": "2026-09-02",
                    "nav": 8.3251,
                    "acc_nav": 8.3251,
                    "daily_return": -1.4,
                    "cum_return": None,
                },
                {
                    "trade_date": "2026-09-01",
                    "nav": 8.4433,
                    "acc_nav": 8.4433,
                    "daily_return": 1.25,
                    "cum_return": None,
                },
                {
                    "trade_date": "2026-08-31",
                    "nav": 8.3391,
                    "acc_nav": 8.3391,
                    "daily_return": None,
                    "cum_return": None,
                },
            ],
        )
        post.assert_called_once()
        call = post.call_args
        self.assertEqual(
            call.args,
            (
                "https://www.howbuy.com/fund/ajax/gmfund/history/huobi.htm?jjdm=002910&flag=1",
            ),
        )
        self.assertEqual(call.kwargs["data"], {"page": 1, "perPage": 10})
        self.assertEqual(call.kwargs["timeout"], (5, 15))
        self.assertFalse(call.kwargs["allow_redirects"])
        self.assertEqual(
            call.kwargs["headers"]["Referer"], "https://www.howbuy.com/fund/002910/"
        )
        self.assertEqual(
            call.kwargs["headers"]["Content-Type"],
            "application/x-www-form-urlencoded",
        )

    def test_filters_window_and_fetches_only_pages_needed(self) -> None:
        responses = [
            _FakeResponse(
                _history_html(
                    [
                        ("2026-09-05", "1.05", "1.05", "0.10%"),
                        ("2026-09-04", "1.04", "1.04", "0.10%"),
                        ("2026-09-03", "1.03", "1.03", "0.10%"),
                    ],
                    all_pages=99,
                )
            ),
            _FakeResponse(
                _history_html(
                    [
                        ("2026-09-02", "1.02", "1.02", "-0.10%"),
                        ("2026-09-01", "1.01", "1.01", "0.20%"),
                        ("2026-08-31", "1.00", "1.00", "-0.30%"),
                    ],
                    all_pages=99,
                )
            ),
        ]

        with patch.object(howbuy.requests, "post", side_effect=responses) as post:
            rows = howbuy.fetch_nav_incremental("002910", "2026-09-01", "2026-09-02")

        self.assertEqual(
            [row["trade_date"] for row in rows], ["2026-09-02", "2026-09-01"]
        )
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].kwargs["data"]["page"], 1)
        self.assertEqual(post.call_args_list[1].kwargs["data"]["page"], 2)

    def test_stops_on_first_page_once_earliest_date_reaches_window_start(self) -> None:
        response = _FakeResponse(
            _history_html(
                [
                    ("2026-09-02", "1.02", "1.02", "0.10%"),
                    ("2026-09-01", "1.01", "1.01", "0.10%"),
                    ("2026-08-31", "1.00", "1.00", "0.10%"),
                ],
                all_pages=233,
            )
        )

        with patch.object(howbuy.requests, "post", return_value=response) as post:
            rows = howbuy.fetch_nav_incremental("002910", "2026-09-01", "2026-09-02")

        self.assertEqual(len(rows), 2)
        post.assert_called_once()

    def test_empty_normal_table_raises_source_empty(self) -> None:
        response = _FakeResponse(_history_html([], all_pages=1))

        with (
            patch.object(howbuy.requests, "post", return_value=response),
            self.assertRaises(NoNavDataError) as raised,
        ):
            howbuy.fetch_nav_incremental("002910", "2026-09-01", "2026-09-02")

        self.assertEqual(raised.exception.reason, HOWBUY_EMPTY)

    def test_redirect_raises_definitive_invalid_code_error(self) -> None:
        response = _FakeResponse(status_code=302)

        with (
            patch.object(howbuy.requests, "post", return_value=response) as post,
            self.assertRaises(NoNavDataError) as raised,
        ):
            howbuy.fetch_nav_incremental("999999", "2026-09-01", "2026-09-02")

        self.assertEqual(raised.exception.reason, HOWBUY_REDIRECT)
        post.assert_called_once()

    def test_money_market_table_raises_source_empty_for_fallback(self) -> None:
        response = _FakeResponse(
            "<table><tr><td>时间</td><td>每万份收益</td>"
            "<td>7日年化收益率</td></tr>"
            "<tr><td>2026-09-02</td><td>0.5123</td><td>1.35%</td></tr></table>"
        )

        with (
            patch.object(howbuy.requests, "post", return_value=response),
            self.assertRaises(NoNavDataError) as raised,
        ):
            howbuy.fetch_nav_incremental("000198", "2026-09-01", "2026-09-02")

        self.assertEqual(raised.exception.reason, HOWBUY_EMPTY)


if __name__ == "__main__":
    import unittest

    unittest.main()
