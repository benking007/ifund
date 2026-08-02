"""Tests for the fund NAV worker's incremental and fallback paths."""
# pylint: disable=missing-function-docstring,protected-access
from __future__ import annotations

from unittest import TestCase
from unittest.mock import patch

from app.fund_nav.fetch import worker


class FundNavWorkerTests(TestCase):
    """Verify the worker's cache, incremental, and fallback decisions."""

    def test_process_one_uses_full_path_when_nav_is_missing(self) -> None:
        with (
            patch.object(worker.calendar_crud, "base_trade_date", return_value="2026-07-31"),
            patch.object(worker.nav_crud, "stored_latest", return_value=None),
            patch.object(worker.eastmoney, "fetch_nav_incremental") as fetch,
            patch.object(worker, "_process_one_akshare_full", return_value="success") as full,
        ):
            result = worker._process_one("000001")

        self.assertEqual(result, "success")
        full.assert_called_once_with("000001")
        fetch.assert_not_called()

    def test_process_one_uses_incremental_rows_and_skips_none_values(self) -> None:
        rows = [
            {
                "trade_date": "2026-07-30",
                "nav": 1.01,
                "acc_nav": 1.21,
                "daily_return": 0.1,
                "cum_return": None,
            },
            {
                "trade_date": "2026-07-31",
                "nav": None,
                "acc_nav": 1.22,
                "daily_return": None,
                "cum_return": 2.3,
            },
        ]

        with (
            patch.object(worker.calendar_crud, "base_trade_date", return_value="2026-07-31"),
            patch.object(worker.nav_crud, "stored_latest", return_value="2026-07-30"),
            patch.object(worker.eastmoney, "fetch_nav_incremental", return_value=rows) as fetch,
            patch.object(worker.nav_crud, "insert_rows") as insert_rows,
        ):
            result = worker._process_one("000001")

        self.assertEqual(result, "success")
        fetch.assert_called_once_with("000001", "2026-07-30", "2026-07-31")
        self.assertEqual(insert_rows.call_count, 2)
        self.assertEqual(insert_rows.call_args_list[0].args[0], "fund_nav")
        self.assertEqual(insert_rows.call_args_list[0].args[1][0], {
            "fund_code": "000001",
            "trade_date": "2026-07-30",
            "nav": 1.01,
            "acc_nav": 1.21,
            "daily_return": 0.1,
            "fetch_time": insert_rows.call_args_list[0].args[1][0]["fetch_time"],
        })
        self.assertEqual(insert_rows.call_args_list[1].args[0], "fund_cum_return")
        self.assertEqual(insert_rows.call_args_list[1].args[1][0]["trade_date"], "2026-07-31")
        self.assertEqual(insert_rows.call_args_list[1].args[1][0]["cum_return"], 2.3)

    def test_process_one_skips_when_nav_is_current(self) -> None:
        with (
            patch.object(worker.calendar_crud, "base_trade_date", return_value="2026-07-31"),
            patch.object(worker.nav_crud, "stored_latest", return_value="2026-07-31"),
            patch.object(worker.eastmoney, "fetch_nav_incremental") as fetch,
            patch.object(worker.nav_crud, "insert_rows") as insert_rows,
        ):
            result = worker._process_one("000001")

        self.assertEqual(result, "skip")
        fetch.assert_not_called()
        insert_rows.assert_not_called()

    def test_process_one_falls_back_to_akshare_after_incremental_error(self) -> None:
        with (
            patch.object(worker.calendar_crud, "base_trade_date", return_value="2026-07-31"),
            patch.object(worker.nav_crud, "stored_latest", return_value="2026-07-30"),
            patch.object(
                worker.eastmoney,
                "fetch_nav_incremental",
                side_effect=RuntimeError("network down"),
            ),
            patch.object(worker, "_process_one_akshare_full", return_value="success") as fallback,
        ):
            result = worker._process_one("000001")

        self.assertEqual(result, "success")
        fallback.assert_called_once_with("000001")

    def test_process_one_akshare_full_falls_back_to_js_after_nav_parse_error(self) -> None:
        js_rows = [{
            "trade_date": "2026-07-31",
            "nav": 1.01,
            "acc_nav": 1.21,
            "daily_return": 0.1,
        }]

        with (
            patch.object(worker.nav_crud, "stored_latest", return_value=None),
            patch.object(worker, "_nav_rows", side_effect=RuntimeError("MiniRacer error")),
            patch.object(worker, "_cum_rows", return_value=[]),
            patch.object(worker.akshare_js, "fetch_nav_js", return_value=js_rows) as fetch,
            patch.object(worker.nav_crud, "insert_rows") as insert_rows,
        ):
            result = worker._process_one_akshare_full("000009")

        self.assertEqual(result, "success")
        fetch.assert_called_once_with("000009")
        self.assertEqual(insert_rows.call_count, 2)
        self.assertEqual(insert_rows.call_args_list[0].args[0], "fund_nav")
        self.assertEqual(insert_rows.call_args_list[0].args[1][0]["fund_code"], "000009")
        self.assertEqual(insert_rows.call_args_list[0].args[1][0]["nav"], 1.01)

    def test_process_one_akshare_full_fails_when_both_sources_have_no_rows(self) -> None:
        with (
            patch.object(worker.nav_crud, "stored_latest", return_value=None),
            patch.object(worker, "_nav_rows", return_value=[]),
            patch.object(worker.akshare_js, "fetch_nav_js", return_value=[]),
            patch.object(worker.nav_crud, "insert_rows") as insert_rows,
        ):
            result = worker._process_one_akshare_full("000012")

        self.assertEqual(result, "fail")
        insert_rows.assert_not_called()


if __name__ == "__main__":
    import unittest

    unittest.main()
