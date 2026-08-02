"""Tests for the East Money F10 incremental NAV fetcher."""
# pylint: disable=missing-function-docstring
from __future__ import annotations

import datetime
from unittest import TestCase
from unittest.mock import patch

from app.fund_nav.fetch import eastmoney


class _FakeResponse:
    """Minimal response double for ``requests.get``."""

    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class EastMoneyFetcherTests(TestCase):
    """Verify request construction, pagination, and field mapping."""

    def test_fetch_nav_incremental_maps_fields_and_paginates_to_total_count(self) -> None:
        responses = [
            _FakeResponse({
                "Data": {
                    "LSJZList": [
                        {
                            "FSRQ": "2026-07-31",
                            "DWJZ": "1.2345",
                            "LJJZ": "1.5678",
                            "JZZZL": "0.12",
                            "ACTUALSYI": "0.34",
                        },
                        {
                            "FSRQ": "2026-07-30",
                            "DWJZ": "1.2300",
                            "LJJZ": "1.5600",
                            "JZZZL": "-0.36",
                            "ACTUALSYI": None,
                        },
                    ],
                    "TotalCount": 501,
                },
                "ErrCode": 0,
                "ErrMsg": "",
            }),
            _FakeResponse({
                "Data": {
                    "LSJZList": [{
                        "FSRQ": "2026-07-29",
                        "DWJZ": "1.2200",
                        "LJJZ": "1.5500",
                        "JZZZL": "-0.81",
                        "ACTUALSYI": "-1.02",
                    }],
                    "TotalCount": 501,
                },
                "ErrCode": 0,
                "ErrMsg": "",
            }),
        ]

        with patch.object(eastmoney.requests, "get", side_effect=responses) as get:
            rows = eastmoney.fetch_nav_incremental("000001", "2026-07-29", "2026-07-31")

        self.assertEqual(rows, [
            {
                "trade_date": "2026-07-31",
                "nav": 1.2345,
                "acc_nav": 1.5678,
                "daily_return": 0.12,
                "cum_return": 0.34,
            },
            {
                "trade_date": "2026-07-30",
                "nav": 1.23,
                "acc_nav": 1.56,
                "daily_return": -0.36,
                "cum_return": None,
            },
            {
                "trade_date": "2026-07-29",
                "nav": 1.22,
                "acc_nav": 1.55,
                "daily_return": -0.81,
                "cum_return": -1.02,
            },
        ])
        self.assertEqual(get.call_count, 2)
        first_call = get.call_args_list[0]
        self.assertEqual(first_call.args, ("https://api.fund.eastmoney.com/f10/lsjz",))
        self.assertEqual(first_call.kwargs["params"], {
            "fundCode": "000001",
            "pageIndex": 1,
            "pageSize": 500,
            "startDate": "2026-07-29",
            "endDate": "2026-07-31",
        })
        self.assertEqual(first_call.kwargs["headers"]["Referer"], "https://fundf10.eastmoney.com/")
        self.assertEqual(first_call.kwargs["timeout"], 15)
        self.assertEqual(get.call_args_list[1].kwargs["params"]["pageIndex"], 2)

    def test_fetch_nav_incremental_stops_when_page_is_empty(self) -> None:
        response = _FakeResponse({
            "Data": {"LSJZList": [], "TotalCount": 1000},
            "ErrCode": 0,
            "ErrMsg": "",
        })

        with patch.object(eastmoney.requests, "get", return_value=response) as get:
            rows = eastmoney.fetch_nav_incremental("000001", "2026-07-29", "2026-07-31")

        self.assertEqual(rows, [])
        self.assertEqual(get.call_count, 1)

    def test_fetch_nav_incremental_handles_gateway_page_size_compatibility(self) -> None:
        responses = [
            _FakeResponse({
                "Data": None,
                "ErrCode": 0,
                "ErrMsg": None,
                "TotalCount": 0,
                "PageSize": 0,
                "PageIndex": 0,
            }),
            _FakeResponse({
                "Data": {"LSJZList": [{
                    "FSRQ": "2026-07-31",
                    "DWJZ": "1.2540",
                    "LJJZ": "3.8270",
                    "JZZZL": "2.70",
                    "ACTUALSYI": "",
                }]},
                "ErrCode": 0,
                "ErrMsg": None,
                "TotalCount": 1,
            }),
        ]

        with patch.object(eastmoney.requests, "get", side_effect=responses) as get:
            rows = eastmoney.fetch_nav_incremental("000001", "2026-07-31", "2026-07-31")

        self.assertEqual(rows[0]["nav"], 1.254)
        self.assertIsNone(rows[0]["cum_return"])
        self.assertEqual(get.call_args_list[0].kwargs["params"]["pageSize"], eastmoney.PAGE_SIZE)
        self.assertEqual(
            get.call_args_list[1].kwargs["params"]["pageSize"],
            eastmoney.FALLBACK_PAGE_SIZE,
        )

    def test_fetch_nav_full_uses_today_as_end_date(self) -> None:
        with patch.object(eastmoney, "fetch_nav_incremental", return_value=[]) as fetch:
            rows = eastmoney.fetch_nav_full("000001")

        self.assertEqual(rows, [])
        fetch.assert_called_once_with(
            "000001", "2000-01-01", datetime.date.today().isoformat())


if __name__ == "__main__":
    import unittest

    unittest.main()
