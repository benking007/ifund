"""Tushare 交易日历主源、降级与统一同步服务测试。"""

from __future__ import annotations

import datetime as dt
from unittest import TestCase
from unittest.mock import patch

from app.trade_calendar import service
from app.trade_calendar.fetch import fetcher


class TradeCalendarFetcherTests(TestCase):
    """网络全部 mock，不消耗 Tushare 调用量。"""

    def test_default_window_is_rolling_nine_years(self) -> None:
        self.assertEqual(
            fetcher.default_window(dt.date(2026, 9, 4)),
            ("2020-01-01", "2028-12-31"),
        )

    def test_tushare_pages_by_year_on_sse(self) -> None:
        pages = [
            [{"cal_date": "20200102", "is_open": 1}],
            [{"cal_date": "20210104", "is_open": 1}],
            [{"cal_date": "20220104", "is_open": 1}],
        ]
        with patch.object(fetcher.tushare_client, "call", side_effect=pages) as call:
            dates, calls = fetcher.fetch_tushare_trade_dates("2020-01-01", "2022-12-31")

        self.assertEqual(calls, 3)
        self.assertEqual(dates, ["2020-01-02", "2021-01-04", "2022-01-04"])
        self.assertTrue(
            all(item.args[0] == "trade_cal" for item in call.call_args_list)
        )
        self.assertTrue(
            all(item.args[1]["exchange"] == "SSE" for item in call.call_args_list)
        )
        self.assertEqual(call.call_args_list[0].args[1]["start_date"], "20200101")
        self.assertEqual(call.call_args_list[-1].args[1]["end_date"], "20221231")

    def test_tushare_failure_falls_back_without_losing_existing_dates(self) -> None:
        with (
            patch.object(
                fetcher,
                "fetch_tushare_trade_dates",
                side_effect=fetcher.TushareCalendarError("upstream down", 1),
            ),
            patch.object(
                fetcher,
                "fetch_sina_trade_dates",
                return_value=["2019-12-31", "2020-01-02"],
            ),
        ):
            result = fetcher.fetch_trade_calendar(
                existing_dates=["2018-01-02", "2027-01-04"],
                start_date="2020-01-01",
                end_date="2028-12-31",
            )

        self.assertEqual(result.source, "akshare.sina_fallback")
        self.assertEqual(result.tushare_calls, 1)
        self.assertEqual(
            result.dates,
            ["2018-01-02", "2019-12-31", "2020-01-02", "2027-01-04"],
        )

    def test_future_year_not_published_is_reported(self) -> None:
        with patch.object(
            fetcher,
            "fetch_tushare_trade_dates",
            return_value=(["2027-01-04", "2027-12-31"], 2),
        ):
            result = fetcher.fetch_trade_calendar(
                existing_dates=["2028-01-04"],
                start_date="2027-01-01",
                end_date="2028-12-31",
            )

        self.assertEqual(result.year_counts, {"2027": 2, "2028": 0})
        self.assertEqual(result.missing_years, [2028])
        self.assertEqual(result.preserved_missing_year_dates, 1)
        self.assertIn("2028-01-04", result.dates)

    def test_service_preserves_history_and_uses_replace_all(self) -> None:
        result = fetcher.CalendarFetchResult(
            dates=["1990-12-19", "2028-12-29"],
            source="tushare.trade_cal",
            exchange="SSE",
            window_start="2020-01-01",
            window_end="2028-12-31",
            tushare_calls=9,
            preserved_outside_window=1,
        )
        with (
            patch.object(
                service.calendar_crud,
                "list_dates",
                return_value=["1990-12-19"],
            ),
            patch.object(
                service.fetcher, "fetch_trade_calendar", return_value=result
            ) as fetch,
            patch.object(
                service.calendar_crud, "replace_all", return_value=2
            ) as replace,
        ):
            summary = service.sync_calendar()

        fetch.assert_called_once_with(existing_dates=["1990-12-19"])
        replace.assert_called_once_with(result.dates)
        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["source"], "tushare.trade_cal")
        self.assertEqual(summary["latest"], "2028-12-29")

    def test_repeated_sync_passes_identical_rows_to_idempotent_replace(self) -> None:
        result = fetcher.CalendarFetchResult(
            dates=["2026-01-05", "2026-01-06"],
            source="tushare.trade_cal",
            exchange="SSE",
            window_start="2020-01-01",
            window_end="2028-12-31",
            tushare_calls=9,
            preserved_outside_window=0,
        )
        with (
            patch.object(service.calendar_crud, "list_dates", return_value=[]),
            patch.object(service.fetcher, "fetch_trade_calendar", return_value=result),
            patch.object(
                service.calendar_crud, "replace_all", return_value=2
            ) as replace,
        ):
            first = service.sync_calendar()
            second = service.sync_calendar()

        self.assertEqual(first, second)
        self.assertEqual(replace.call_count, 2)
        self.assertEqual(replace.call_args_list[0].args, replace.call_args_list[1].args)
