"""retry_nav 按缺口窗口回补：合并、回退、自停和 dry-run。"""

from __future__ import annotations

# 用例名已经说明断言，不再逐个补文档字符串。
# pylint: disable=missing-function-docstring
import datetime as dt
from contextlib import ExitStack
from unittest import TestCase
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.fund_nav import nav_window_retry
from app.fund_nav.fetch import tushare_client
from scripts import retry_nav_repair_queue as retry_script


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _at(hour: int, minute: int, day: int = 20) -> dt.datetime:
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=SHANGHAI)


class SequenceClock:
    """按调用顺序返回预设时刻，用尽后停在最后一次。"""

    def __init__(self, values: list[dt.datetime]) -> None:
        self.values = list(values)
        self.calls = 0

    def __call__(self) -> dt.datetime:
        index = min(self.calls, len(self.values) - 1)
        self.calls += 1
        return self.values[index]


def _nav_row(task_id: int, code: str, start: str | None, end: str | None) -> dict:
    return {
        "id": task_id,
        "fund_code": code,
        "task_kind": "nav",
        "gap_start": start,
        "gap_end": end,
        "last_error": "服务异常，请稍后再试",
    }


def _source(day: str, nav: float) -> dict:
    compact = day.replace("-", "")
    return {
        "ts_code": "110011.OF",
        "nav_date": compact,
        "unit_nav": nav,
        "accum_nav": nav + 0.1,
        "adj_nav": nav + 0.2,
    }


class WindowPlanTests(TestCase):
    """纯规划：相邻合并、远距保持、无日期回退。"""

    def test_merge_adjacent_and_overlapping_windows(self) -> None:
        rows = [
            _nav_row(1, "110011", "2024-01-02", "2024-01-05"),
            _nav_row(2, "110011", "2024-01-08", "2024-01-10"),
            _nav_row(3, "110011", "2024-01-04", "2024-01-06"),
            _nav_row(4, "110011", "2024-03-01", "2024-03-03"),
            _nav_row(5, "110022", "2024-01-08", "2024-01-08"),
        ]
        work = nav_window_retry.plan_nav_work(rows)
        windows = {
            (item["fund_code"], item["start"].isoformat(), item["end"].isoformat()): [
                row["id"] for row in item["rows"]
            ]
            for item in work
            if item["mode"] == nav_window_retry.MODE_WINDOW
        }
        self.assertEqual(windows[("110011", "2024-01-02", "2024-01-10")], [1, 3, 2])
        self.assertEqual(windows[("110011", "2024-03-01", "2024-03-03")], [4])
        self.assertEqual(windows[("110022", "2024-01-08", "2024-01-08")], [5])
        self.assertEqual(len(windows), 3)

    def test_missing_or_sentinel_dates_are_full_history(self) -> None:
        rows = [
            _nav_row(1, "110099", None, None),
            _nav_row(2, "110099", "2000-01-01", "2999-12-31"),
            _nav_row(3, "110088", "2024-01-02", "2999-12-31"),
        ]
        work = nav_window_retry.plan_nav_work(rows)
        self.assertEqual(len(work), 2)
        self.assertTrue(all(item["mode"] == nav_window_retry.MODE_FULL_HISTORY for item in work))
        by_code = {item["fund_code"]: [row["id"] for row in item["rows"]] for item in work}
        self.assertEqual(by_code["110099"], [1, 2])
        self.assertEqual(by_code["110088"], [3])

    def test_stop_deadline_rolls_to_next_morning(self) -> None:
        started = _at(22, 0)
        deadline = nav_window_retry.resolve_deadline(started, dt.time(7, 50))
        self.assertEqual(deadline, _at(7, 50, day=21))
        self.assertFalse(nav_window_retry.should_stop(
            _at(23, 30), started=started, deadline=deadline, max_minutes=None,
        ))
        self.assertTrue(nav_window_retry.should_stop(
            _at(7, 50, day=21), started=started, deadline=deadline, max_minutes=None,
        ))
        self.assertTrue(nav_window_retry.should_stop(
            _at(22, 10), started=started, deadline=None, max_minutes=5,
        ))


class _NavDoubles:
    """挡住 Tushare、全史回补和队列写库。"""

    def __init__(self) -> None:
        self.stored: list[dict] = []
        self.done: list[int] = []
        self.failed: list[tuple[int, str]] = []

    def upsert(self, rows: list[dict]) -> int:
        self.stored.extend(rows)
        return len(rows)

    def list_rows(self, code: str, *, start_date: str | None = None, end_date: str | None = None):
        return [
            row for row in self.stored
            if row["fund_code"] == code
            and (start_date is None or row["trade_date"] >= start_date)
            and (end_date is None or row["trade_date"] <= end_date)
        ]

    def mark_done(self, task_id: int) -> None:
        self.done.append(task_id)

    def mark_failed(self, task_id: int, error: str, **_kwargs) -> None:
        self.failed.append((task_id, error))

    def patch(self, *, fetch=None, backfill=None):
        """返回按固定顺序排列的 mock：fetch、upsert、list、done、failed、backfill。"""
        fetch = fetch if fetch is not None else []
        backfill = backfill if backfill is not None else {
            "fund_code": "110099", "status": "success", "rows": 1,
        }
        return (
            patch.object(nav_window_retry.tushare_client, "fetch_fund_nav", return_value=fetch),
            patch.object(nav_window_retry.nav_crud, "upsert_nav_rows", side_effect=self.upsert),
            patch.object(nav_window_retry.nav_crud, "list_nav_rows", side_effect=self.list_rows),
            patch.object(nav_window_retry.repair_crud, "mark_done", side_effect=self.mark_done),
            patch.object(nav_window_retry.repair_crud, "mark_failed", side_effect=self.mark_failed),
            patch.object(nav_window_retry.backfill_worker, "backfill_one", return_value=backfill),
        )

    def enter(self, stack: ExitStack, *, fetch=None, backfill=None):
        """进入全部 mock，并按与 patch() 相同的顺序返回。"""
        return [stack.enter_context(item) for item in self.patch(fetch=fetch, backfill=backfill)]


class RetryNavApplyTests(TestCase):
    """mock Tushare 后的执行路径。"""

    def test_same_fund_rows_use_one_window_request(self) -> None:
        rows = [
            _nav_row(1, "110011", "2024-01-02", "2024-01-05"),
            _nav_row(2, "110011", "2024-01-08", "2024-01-10"),
        ]
        source = [
            _source("2024-01-02", 1.1),
            _source("2024-01-05", 1.2),
            _source("2024-01-08", 1.3),
            _source("2024-01-10", 1.4),
            _source("2019-01-01", 0.5),
        ]
        doubles = _NavDoubles()
        with ExitStack() as stack:
            fetch, _upsert, _listed, _done, _failed, backfill = doubles.enter(stack, fetch=source)
            report = retry_script.build_report(
                rows, bucket="retry_nav", limit=50, apply=True,
            )
        fetch.assert_called_once_with("110011", "2024-01-02", "2024-01-10")
        backfill.assert_not_called()
        self.assertEqual(
            [row["trade_date"] for row in doubles.stored],
            ["2024-01-02", "2024-01-05", "2024-01-08", "2024-01-10"],
        )
        self.assertEqual(doubles.stored[0]["nav"], 1.1)
        self.assertEqual(doubles.stored[0]["adj_src"], "tushare")
        self.assertEqual(doubles.done, [1, 2])
        self.assertEqual(doubles.failed, [])
        self.assertEqual(report["nav_retry"]["windowed_rows"], 2)
        self.assertEqual(report["nav_retry"]["windowed_requests"], 1)
        self.assertEqual(report["nav_retry"]["windowed_calls"], 1)
        self.assertEqual(report["nav_retry"]["full_history_rows"], 0)
        self.assertEqual(report["failed"], 0)

    def test_undated_rows_fall_back_once_per_fund(self) -> None:
        rows = [
            _nav_row(7, "110099", None, None),
            _nav_row(8, "110099", "2000-01-01", "2999-12-31"),
            _nav_row(9, "110011", "2024-02-01", "2024-02-02"),
        ]
        doubles = _NavDoubles()
        source = [_source("2024-02-01", 1.0), _source("2024-02-02", 1.1)]
        with ExitStack() as stack:
            fetch, _upsert, _listed, _done, _failed, backfill = doubles.enter(stack, fetch=source)
            report = retry_script.build_report(
                rows, bucket="retry_nav", limit=50, apply=True,
            )
        backfill.assert_called_once_with("110099")
        fetch.assert_called_once_with("110011", "2024-02-01", "2024-02-02")
        self.assertEqual(report["nav_retry"]["full_history_rows"], 2)
        self.assertEqual(report["nav_retry"]["full_history_requests"], 1)
        self.assertEqual(report["nav_retry"]["full_history_calls"], 1)
        self.assertEqual(report["nav_retry"]["windowed_rows"], 1)
        self.assertEqual(report["nav_retry"]["windowed_requests"], 1)
        self.assertCountEqual(doubles.done, [7, 8, 9])

    def test_window_failure_keeps_safe_last_error(self) -> None:
        rows = [_nav_row(4, "110011", "2024-04-01", "2024-04-03")]
        doubles = _NavDoubles()
        with ExitStack() as stack:
            fetch, upsert, _listed, mark_done, _failed, _backfill = doubles.enter(stack)
            fetch.side_effect = RuntimeError("110011")
            report = retry_script.build_report(
                rows, bucket="retry_nav", limit=50, apply=True,
            )
        upsert.assert_not_called()
        mark_done.assert_not_called()
        self.assertEqual(doubles.failed, [(4, "治理任务失败")])
        self.assertEqual(report["results"][0]["error"], "治理任务失败")
        self.assertEqual(report["results"][0]["result"], "failed")
        self.assertNotEqual(report["results"][0]["error"], "110011")

    def test_incomplete_write_stays_failed(self) -> None:
        rows = [_nav_row(5, "110011", "2024-05-06", "2024-05-06")]
        doubles = _NavDoubles()
        with ExitStack() as stack:
            _fetch, _upsert, listed, mark_done, _failed, _backfill = doubles.enter(
                stack, fetch=[_source("2024-05-06", 1.5)],
            )
            listed.side_effect = lambda *_args, **_kwargs: []
            report = retry_script.build_report(
                rows, bucket="retry_nav", limit=50, apply=True,
            )
        mark_done.assert_not_called()
        self.assertEqual(doubles.failed, [(5, "窗口写入不完整")])
        self.assertEqual(report["failed"], 1)

    def test_stop_at_does_not_start_the_next_request(self) -> None:
        rows = [
            _nav_row(1, "110011", "2024-06-03", "2024-06-03"),
            _nav_row(2, "110022", "2024-06-04", "2024-06-04"),
        ]
        clock = SequenceClock([_at(7, 40), _at(7, 45), _at(7, 51)])
        doubles = _NavDoubles()
        with ExitStack() as stack:
            fetch, _upsert, _listed, _done, mark_failed, backfill = doubles.enter(
                stack, fetch=[_source("2024-06-03", 1.0)],
            )
            report = retry_script.build_report(
                rows,
                bucket="retry_nav",
                limit=50,
                apply=True,
                stop_at=dt.time(7, 50),
                clock=clock,
            )
        fetch.assert_called_once_with("110011", "2024-06-03", "2024-06-03")
        backfill.assert_not_called()
        mark_failed.assert_not_called()
        self.assertEqual(doubles.done, [1])
        self.assertEqual(report["nav_retry"]["windowed_requests"], 2)
        self.assertEqual(report["nav_retry"]["windowed_calls"], 1)
        self.assertEqual(report["nav_retry"]["stopped_rows"], 1)
        stopped = [item for item in report["results"] if item["result"] == "stopped"]
        self.assertEqual([item["id"] for item in stopped], [2])
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["applied"], 1)

    def test_max_minutes_stops_before_later_rows(self) -> None:
        rows = [
            _nav_row(1, "110011", "2024-07-01", "2024-07-01"),
            _nav_row(2, "110022", "2024-07-02", "2024-07-02"),
        ]
        clock = SequenceClock([_at(1, 0), _at(1, 2), _at(1, 12)])
        doubles = _NavDoubles()
        with ExitStack() as stack:
            fetch, _upsert, _listed, _done, _failed, _backfill = doubles.enter(
                stack, fetch=[_source("2024-07-01", 1.0)],
            )
            report = retry_script.build_report(
                rows,
                bucket="retry_nav",
                limit=10,
                apply=True,
                max_minutes=10,
                clock=clock,
            )
        fetch.assert_called_once()
        self.assertEqual(report["stopped"], 1)
        self.assertEqual(report["results"][1]["result"], "stopped")

    def test_dry_run_does_not_write(self) -> None:
        rows = [
            _nav_row(1, "110011", "2024-01-02", "2024-01-05"),
            _nav_row(2, "110011", "2024-01-08", "2024-01-09"),
            _nav_row(3, "110099", None, None),
        ]
        doubles = _NavDoubles()
        with ExitStack() as stack:
            fetch, upsert, _listed, mark_done, mark_failed, backfill = doubles.enter(stack)
            report = retry_script.build_report(
                rows, bucket="retry_nav", limit=50, apply=False,
            )
        fetch.assert_not_called()
        upsert.assert_not_called()
        mark_done.assert_not_called()
        mark_failed.assert_not_called()
        backfill.assert_not_called()
        self.assertTrue(report["dry_run"])
        self.assertNotIn("results", report)
        self.assertEqual(report["nav_retry"]["windowed_rows"], 2)
        self.assertEqual(report["nav_retry"]["windowed_requests"], 1)
        self.assertEqual(report["nav_retry"]["full_history_rows"], 1)
        self.assertEqual(report["nav_retry"]["windowed_calls"], 0)
        self.assertEqual(report["planned"][0]["id"], 1)

    def test_limit_is_applied_before_merge(self) -> None:
        rows = [
            _nav_row(1, "110011", "2024-01-02", "2024-01-03"),
            _nav_row(2, "110011", "2024-01-04", "2024-01-05"),
        ]
        report = retry_script.build_report(rows, bucket="retry_nav", limit=1, apply=False)
        self.assertEqual(report["nav_retry"]["windowed_rows"], 1)
        self.assertEqual(report["nav_retry"]["windowed_requests"], 1)
        self.assertEqual(report["nav_retry"]["preview"][0]["ids"], [1])

    def test_fetch_fund_nav_sends_start_and_end(self) -> None:
        with (
            patch.object(tushare_client, "resolve_ts_code", return_value="110011.OF"),
            patch.object(tushare_client, "call", return_value=[]) as call,
            patch.object(tushare_client.requests, "post") as post,
        ):
            tushare_client.fetch_fund_nav("110011", "2024-01-02", "2024-01-10")
        post.assert_not_called()
        self.assertEqual(call.call_args.args[0], "fund_nav")
        self.assertEqual(call.call_args.args[1]["ts_code"], "110011.OF")
        self.assertEqual(call.call_args.args[1]["start_date"], "20240102")
        self.assertEqual(call.call_args.args[1]["end_date"], "20240110")
