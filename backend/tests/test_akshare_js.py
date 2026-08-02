"""Tests for the direct East Money JavaScript NAV parser."""
# pylint: disable=missing-function-docstring
from __future__ import annotations

from unittest import TestCase
from unittest.mock import patch

from app.fund_nav.fetch import akshare_js


class _FakeResponse:
    """Minimal response double for ``requests.get``."""

    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


class AkshareJsFetcherTests(TestCase):
    """Verify both normal and money-fund JavaScript layouts."""

    def test_fetch_nav_js_aligns_acc_nav_and_converts_string_values(self) -> None:
        javascript = """
        var Data_netWorthTrend = [
            {"x":1704067200000,"y":"1.2","equityReturn":null,"unitMoney":""},
            {"x":1704153600000,"y":1.3,"equityReturn":"0.5","unitMoney":""}
        ];
        var Data_ACWorthTrend = [[1704067200000,"1.4"],[1704153600000,1.5]];
        """

        with patch.object(akshare_js.requests, "get", return_value=_FakeResponse(javascript)):
            rows = akshare_js.fetch_nav_js("000001")

        self.assertEqual(rows, [
            {
                "trade_date": "2024-01-01",
                "nav": 1.2,
                "acc_nav": 1.4,
                "daily_return": None,
            },
            {
                "trade_date": "2024-01-02",
                "nav": 1.3,
                "acc_nav": 1.5,
                "daily_return": 0.5,
            },
        ])

    def test_fetch_nav_js_uses_money_fund_income_when_nav_trend_is_missing(self) -> None:
        javascript = (
            'var Data_millionCopiesIncome = '
            '[[1704067200000,"1.2"],[1704153600000,1.3]];'
        )

        with patch.object(akshare_js.requests, "get", return_value=_FakeResponse(javascript)):
            rows = akshare_js.fetch_nav_js("000009")

        self.assertEqual(rows, [
            {
                "trade_date": "2024-01-01",
                "nav": 1.2,
                "acc_nav": None,
                "daily_return": None,
            },
            {
                "trade_date": "2024-01-02",
                "nav": 1.3,
                "acc_nav": None,
                "daily_return": None,
            },
        ])

    def test_fetch_nav_js_returns_empty_for_missing_data(self) -> None:
        with patch.object(
            akshare_js.requests,
            "get",
            return_value=_FakeResponse("var fS_code = '000012';"),
        ):
            rows = akshare_js.fetch_nav_js("000012")

        self.assertEqual(rows, [])


if __name__ == "__main__":
    import unittest

    unittest.main()
