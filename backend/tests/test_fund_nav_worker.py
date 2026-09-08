"""Tests for the fund NAV worker's incremental and fallback paths."""

# pylint: disable=missing-function-docstring,protected-access
from __future__ import annotations

from unittest import TestCase
from unittest.mock import patch

import requests

from app.fund_nav.fetch import worker
from app.fund_nav.fetch.errors import AKSHARE_EMPTY, F10_404, NoNavDataError


class FundNavWorkerTests(TestCase):
    """Verify the worker's cache, incremental, and fallback decisions."""

    def test_process_one_uses_eastmoney_full_window_when_nav_is_missing(self) -> None:
        rows = [
            {
                "trade_date": "2026-07-31",
                "nav": 1.01,
                "acc_nav": 1.21,
                "daily_return": 0.1,
                "cum_return": None,
            }
        ]
        with (
            patch.object(worker.no_nav_blacklist, "is_blacklisted", return_value=False),
            patch.object(
                worker.calendar_crud, "base_trade_date", return_value="2026-07-31"
            ),
            patch.object(worker.nav_crud, "stored_latest", return_value=None),
            patch.object(worker, "_fetch_incremental", return_value=rows) as fetch,
            patch.object(worker, "_write_nav_with_watermark") as write,
        ):
            result = worker._process_one("000001")

        self.assertEqual(result, "success")
        fetch.assert_called_once_with("000001", "2000-01-01", "2026-07-31")
        write.assert_called_once()

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
            patch.object(worker.no_nav_blacklist, "is_blacklisted", return_value=False),
            patch.object(
                worker.calendar_crud, "base_trade_date", return_value="2026-07-31"
            ),
            patch.object(worker.nav_crud, "stored_latest", return_value="2026-07-30"),
            patch.object(worker, "_fetch_incremental", return_value=rows) as fetch,
            patch.object(worker, "_write_nav_with_watermark") as write,
        ):
            result = worker._process_one("000001")

        self.assertEqual(result, "success")
        fetch.assert_called_once_with("000001", "2026-07-30", "2026-07-31")
        nav_rows, cum_rows = write.call_args.args[1:]
        self.assertEqual(
            nav_rows[0],
            {
                "fund_code": "000001",
                "trade_date": "2026-07-30",
                "nav": 1.01,
                "acc_nav": 1.21,
                "daily_return": 0.1,
                "fetch_time": nav_rows[0]["fetch_time"],
            },
        )
        self.assertEqual(cum_rows[0]["trade_date"], "2026-07-31")
        self.assertEqual(cum_rows[0]["cum_return"], 2.3)

    def test_process_one_skips_when_nav_is_current(self) -> None:
        with (
            patch.object(worker.no_nav_blacklist, "is_blacklisted", return_value=False),
            patch.object(
                worker.calendar_crud, "base_trade_date", return_value="2026-07-31"
            ),
            patch.object(worker.nav_crud, "stored_latest", return_value="2026-07-31"),
            patch.object(worker, "_fetch_incremental") as fetch,
            patch.object(worker.nav_crud, "insert_rows") as insert_rows,
        ):
            result = worker._process_one("000001")

        self.assertEqual(result, "skip")
        fetch.assert_not_called()
        insert_rows.assert_not_called()

    def test_process_one_skips_from_preloaded_watermark_without_latest_query(
        self,
    ) -> None:
        with (
            patch.object(worker.no_nav_blacklist, "is_blacklisted", return_value=False),
            patch.object(
                worker.calendar_crud, "base_trade_date", return_value="2026-07-31"
            ),
            patch.object(worker.nav_crud, "stored_latest") as stored,
            patch.object(worker, "_fetch_incremental") as fetch,
        ):
            result = worker._process_one("000001", "2026-07-31")

        self.assertEqual(result, "skip")
        stored.assert_not_called()
        fetch.assert_not_called()

    def test_process_one_advances_watermark_to_actual_latest_row(self) -> None:
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
                "nav": 1.02,
                "acc_nav": 1.22,
                "daily_return": 0.2,
                "cum_return": None,
            },
        ]
        with (
            patch.object(worker.no_nav_blacklist, "is_blacklisted", return_value=False),
            patch.object(
                worker.calendar_crud, "base_trade_date", return_value="2026-08-03"
            ),
            patch.object(worker.nav_crud, "stored_latest") as stored,
            patch.object(worker, "_fetch_incremental", return_value=rows) as fetch,
            patch.object(worker, "_write_nav_with_watermark") as write,
        ):
            result = worker._process_one("000001", "2026-07-29")

        self.assertEqual(result, "success")
        stored.assert_not_called()
        fetch.assert_called_once_with("000001", "2026-07-29", "2026-08-03")
        self.assertEqual(write.call_args.args[0], "000001")
        self.assertEqual(write.call_args.args[1][-1]["trade_date"], "2026-07-31")

    def test_process_one_falls_back_to_akshare_after_incremental_error(self) -> None:
        with (
            patch.object(worker.no_nav_blacklist, "is_blacklisted", return_value=False),
            patch.object(
                worker.calendar_crud, "base_trade_date", return_value="2026-07-31"
            ),
            patch.object(worker.nav_crud, "stored_latest", return_value="2026-07-30"),
            patch.object(
                worker, "_fetch_incremental", side_effect=RuntimeError("network down")
            ),
            patch.object(worker, "_akshare_enabled", return_value=True),
            patch.object(
                worker, "_process_one_akshare_full", return_value="success"
            ) as fallback,
        ):
            result = worker._process_one("000001")

        self.assertEqual(result, "success")
        fallback.assert_called_once_with("000001")

    def test_process_one_akshare_full_skips_js_parse_error_without_js_fallback(
        self,
    ) -> None:
        class JSParseException(Exception):
            """Match AkShare's business parser exception by class name."""

        with (
            patch.object(worker.no_nav_blacklist, "is_blacklisted", return_value=False),
            patch.object(worker.nav_crud, "stored_latest", return_value=None),
            patch.object(
                worker, "_nav_rows", side_effect=JSParseException("404 html")
            ) as fetch,
            patch.object(worker.no_nav_blacklist, "record") as record,
            patch.object(worker.nav_crud, "insert_rows") as insert_rows,
        ):
            result = worker._process_one_akshare_full("000009")

        self.assertEqual(result, "skip")
        fetch.assert_called_once()
        record.assert_not_called()
        insert_rows.assert_not_called()

    def test_process_one_akshare_full_blacklists_empty_nav_without_retry(self) -> None:
        with (
            patch.object(worker.no_nav_blacklist, "is_blacklisted", return_value=False),
            patch.object(worker.nav_crud, "stored_latest", return_value=None),
            patch.object(
                worker,
                "_nav_rows",
                side_effect=NoNavDataError("empty", reason=AKSHARE_EMPTY),
            ) as fetch,
            patch.object(worker.no_nav_blacklist, "record") as record,
            patch.object(worker.nav_crud, "insert_rows") as insert_rows,
        ):
            result = worker._process_one_akshare_full("000012")

        self.assertEqual(result, "skip")
        fetch.assert_called_once()
        record.assert_not_called()
        insert_rows.assert_not_called()

    def test_process_one_f10_404_skips_without_blacklist_or_fallback(self) -> None:
        with (
            patch.object(worker.no_nav_blacklist, "is_blacklisted", return_value=False),
            patch.object(
                worker.calendar_crud, "base_trade_date", return_value="2026-07-31"
            ),
            patch.object(worker.nav_crud, "stored_latest", return_value="2026-07-30"),
            patch.object(
                worker.eastmoney,
                "fetch_nav_incremental",
                side_effect=NoNavDataError("404", reason=F10_404),
            ) as fetch,
            patch.object(worker.no_nav_blacklist, "record") as record,
            patch.object(worker, "_process_one_akshare_full") as fallback,
        ):
            result = worker._process_one("000012")

        self.assertEqual(result, "skip")
        fetch.assert_called_once()
        record.assert_not_called()
        fallback.assert_not_called()

    def test_call_with_retry_retries_only_network_errors_up_to_three_attempts(
        self,
    ) -> None:
        network = requests.Timeout("slow")
        with (
            patch.object(worker.time, "sleep"),
            patch.object(worker.random, "uniform", return_value=1),
        ):
            calls = []

            def fail_network():
                calls.append(None)
                raise network

            with self.assertRaises(worker.NetworkRetryExhausted):
                worker._call_with_retry("network", fail_network)

        self.assertEqual(len(calls), 3)

        class JSParseException(Exception):
            """AkShare business parser error."""

        calls.clear()

        def fail_business():
            calls.append(None)
            raise JSParseException("bad payload")

        with self.assertRaises(JSParseException):
            worker._call_with_retry("business", fail_business)
        self.assertEqual(len(calls), 1)

    def test_process_one_blacklist_hit_skips_all_fetches(self) -> None:
        with (
            patch.object(worker.no_nav_blacklist, "is_blacklisted", return_value=True),
            patch.object(worker.calendar_crud, "base_trade_date") as calendar,
            patch.object(worker.nav_crud, "stored_latest") as stored,
        ):
            result = worker._process_one("000012")

        self.assertEqual(result, "skip")
        calendar.assert_not_called()
        stored.assert_not_called()


if __name__ == "__main__":
    import unittest

    unittest.main()
