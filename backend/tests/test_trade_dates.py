"""最近交易日 API：查表返回 ≤ 今天的最大日期。"""
from __future__ import annotations

import datetime
import unittest
from unittest.mock import patch

from app.main import app
from app.trade_dates.api.router import latest_trade_date


class LatestTradeDateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = app.test_client()

    def test_query_max_on_or_before_today(self) -> None:
        today = datetime.date.today().isoformat()
        got = latest_trade_date(today)
        self.assertIsNotNone(got)
        self.assertLessEqual(got, today)
        self.assertRegex(got, r"^\d{4}-\d{2}-\d{2}$")

    def test_api_returns_same_as_query(self) -> None:
        expected = latest_trade_date()
        self.assertIsNotNone(expected)
        resp = self.client.get("/api/trade-dates/latest")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        payload = resp.get_json()
        self.assertEqual(payload["trade_date"], expected)
        self.assertLessEqual(payload["trade_date"], datetime.date.today().isoformat())

    def test_empty_or_error_is_404(self) -> None:
        with patch("app.trade_dates.api.router.latest_trade_date", return_value=None):
            resp = self.client.get("/api/trade-dates/latest")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("detail", resp.get_json())


if __name__ == "__main__":
    unittest.main()
