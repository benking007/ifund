"""净值日常同步源优先级与状态边界测试；所有网络均为 mock。"""

from __future__ import annotations

# pylint: disable=missing-function-docstring,protected-access
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import ANY, MagicMock, patch

from app.fund_nav import daily_sync
from app.fund_nav.fetch import worker


def tushare_row(code: str = "000001.OF", day: str = "20260903") -> dict:
    return {
        "ts_code": code,
        "nav_date": day,
        "unit_nav": "1.02",
        "accum_nav": "1.12",
        "adj_nav": "1.22",
    }


class DailyNavSourceTests(TestCase):
    """验证 Tushare 窗口、东财补漏和 AkShare 显式开关。"""

    def test_tushare_uses_nav_date_market_pages(self) -> None:
        pages = [[tushare_row(), tushare_row("000002.OF")], [tushare_row("000003.OF")]]
        budget = daily_sync.ApiBudget(3)
        with patch.object(
            daily_sync.tushare_client, "call", side_effect=pages
        ) as api_call:
            rows = daily_sync.fetch_tushare_date("2026-09-03", budget, page_size=2)

        self.assertEqual(len(rows), 3)
        self.assertEqual(budget.used, 2)
        self.assertEqual(
            api_call.call_args_list[0].args[1],
            {
                "nav_date": "20260903",
                "limit": 2,
                "offset": 0,
            },
        )
        self.assertEqual(api_call.call_args_list[1].args[1]["offset"], 2)

    def test_mapping_keeps_adj_and_filters_nan(self) -> None:
        rows = [tushare_row(), tushare_row("000002.OF"), tushare_row("999999.OF")]
        rows[1]["unit_nav"] = float("nan")
        mapped, quarantine = daily_sync.map_tushare_rows(
            "2026-09-03", rows, {"000001.OF": "000001", "000002.OF": "000002"}
        )

        self.assertEqual([row["fund_code"] for row in mapped], ["000001"])
        self.assertEqual(mapped[0]["adj_nav"], 1.22)
        self.assertEqual(mapped[0]["adj_src"], "tushare")
        self.assertEqual(quarantine["missing_or_nan_nav"], 1)
        self.assertEqual(quarantine["unmapped"], 1)

    def test_latest_missing_adj_uses_calc_without_extra_source_call(self) -> None:
        source = tushare_row()
        source["adj_nav"] = None
        mapped, _ = daily_sync.map_tushare_rows(
            "2026-09-03", [source], {"000001.OF": "000001"}
        )

        filled = daily_sync.apply_latest_adj_fallback(mapped, "2026-09-03")

        self.assertEqual(filled, 1)
        self.assertEqual(mapped[0]["adj_nav"], mapped[0]["nav"])
        self.assertEqual(mapped[0]["adj_src"], "calc")

    def test_existing_difference_is_warning_only(self) -> None:
        mapped, _ = daily_sync.map_tushare_rows(
            "2026-09-03", [tushare_row()], {"000001.OF": "000001"}
        )
        result = daily_sync.validate_existing_rows(mapped, {"000001": {"nav": 1.00}})

        self.assertEqual(result["warning_count"], 1)
        self.assertIn("never overwritten", result["write_policy"])

    def test_fact_and_watermark_share_one_commit(self) -> None:
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.rowcount = 1
        inserted = daily_sync.insert_rows_and_advance_watermarks(
            connection,
            [
                {
                    "fund_code": "000001",
                    "trade_date": "2026-09-03",
                    "nav": 1.02,
                    "acc_nav": 1.12,
                    "daily_return": None,
                    "adj_nav": 1.22,
                    "adj_src": "tushare",
                    "fetch_time": "2026-09-04T08:00:00+08:00",
                }
            ],
        )

        self.assertEqual(inserted, 1)
        self.assertEqual(cursor.executemany.call_count, 2)
        fact_sql = cursor.executemany.call_args_list[0].args[0]
        state_sql = cursor.executemany.call_args_list[1].args[0]
        self.assertIn("INSERT IGNORE INTO fund_nav", fact_sql)
        self.assertNotIn("UPDATE", fact_sql.upper())
        self.assertIn("fund_sync_state", state_sql)
        connection.commit.assert_called_once_with()
        connection.rollback.assert_not_called()

    def test_qdii_fof_are_expected_gaps_not_fallback(self) -> None:
        funds = {
            "000001": {"type": "QDII-美股", "name": "海外"},
            "000002": {"type": "FOF", "name": "养老"},
            "000003": {"type": "混合型", "name": "普通基金"},
        }
        fallback, expected = daily_sync.classify_missing(funds, {}, set())

        self.assertEqual(fallback, ["000003"])
        self.assertEqual(expected["000001"], "expected_qdii_disclosure_lag")
        self.assertEqual(expected["000002"], "expected_fof_disclosure_lag")

    def test_continuity_gap_detects_middle_date_for_target_complete_fund(self) -> None:
        gaps = daily_sync.continuity_gap_keys(
            {"000001"},
            ["2026-09-01", "2026-09-02", "2026-09-03"],
            {
                "2026-09-01": {"000001"},
                "2026-09-02": set(),
                "2026-09-03": {"000001"},
            },
        )

        self.assertEqual(gaps, [("000001", "2026-09-02")])

    def test_akshare_is_not_called_when_switch_is_off(self) -> None:
        health = daily_sync.SourceHealth()
        with patch.object(daily_sync, "ProcessPoolExecutor") as executor:
            rows, failures = daily_sync.fetch_akshare_missing(
                ["000001"], "2026-09-03", "2026-09-03", health, enabled=False
            )

        self.assertEqual(rows, [])
        self.assertEqual(failures, [])
        executor.assert_not_called()

    def test_night_target_probes_back_until_a_nonempty_date(self) -> None:
        connection = MagicMock()
        health = daily_sync.SourceHealth()
        budget = daily_sync.ApiBudget(10)
        with (
            patch.object(
                daily_sync,
                "trade_dates",
                return_value=["2026-09-04", "2026-09-03"],
            ),
            patch.object(
                daily_sync,
                "fetch_tushare_date",
                side_effect=[[], [tushare_row()]],
            ) as fetch,
        ):
            target, rows = daily_sync.resolve_night_target(
                connection, "2026-09-04", budget, health
            )

        self.assertEqual(target, "2026-09-03")
        self.assertEqual(rows, [tushare_row()])
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(health.tushare_failed_dates, 1)
        self.assertEqual(health.tushare_success_dates, 1)

    def test_incremental_round_is_idempotent_and_skips_full_work(self) -> None:
        connection = MagicMock()
        with tempfile.TemporaryDirectory() as directory:
            plan = Path(directory) / "plan.json"
            with (
                patch.object(
                    daily_sync,
                    "resolve_night_target",
                    return_value=("2026-09-03", [tushare_row()]),
                ),
                patch.object(
                    daily_sync,
                    "load_mapping",
                    return_value=(
                        {"000001.OF": "000001"},
                        {"000001": {"ts_code": "000001.OF", "status": "L"}},
                    ),
                ),
                patch.object(
                    daily_sync,
                    "load_existing_rows",
                    side_effect=[{}, {"000001": {"nav": 1.02}}],
                ),
                patch.object(
                    daily_sync,
                    "insert_rows_and_advance_watermarks",
                    side_effect=[1, 0],
                ) as write,
                patch.object(daily_sync, "_fallback_and_write") as fallback,
                patch.object(daily_sync, "integrity_gate") as gate,
            ):
                first = daily_sync.run_incremental(
                    connection,
                    today="2026-09-04",
                    plan_path=plan,
                )
                second = daily_sync.run_incremental(
                    connection,
                    today="2026-09-04",
                    plan_path=plan,
                )

        self.assertEqual(first["tushare_inserted"], 1)
        self.assertEqual(second["tushare_inserted"], 0)
        self.assertEqual(second["noop_streak"], 1)
        self.assertEqual(write.call_args_list[1].args[1], [])
        fallback.assert_not_called()
        gate.assert_not_called()
        self.assertTrue(second["eastmoney_skipped"])
        self.assertTrue(second["integrity_gate_skipped"])

    def test_poll_state_detects_target_date_switch(self) -> None:
        old_pull = daily_sync.NightWindowPull(
            "2026-09-03", 10, 10, 0, 10, {}, 0, {}, {}
        )
        new_pull = daily_sync.NightWindowPull("2026-09-04", 2, 2, 2, 0, {}, 0, {}, {})
        with tempfile.TemporaryDirectory() as directory:
            plan = Path(directory) / "state.json"
            daily_sync._advance_poll_state(plan, today="2026-09-04", pull=old_pull)
            result = daily_sync._advance_poll_state(
                plan, today="2026-09-04", pull=new_pull
            )

        self.assertTrue(result["target_changed"])
        self.assertEqual(result["noop_streak"], 0)

    def test_finalize_runs_fallback_gate_and_health_report_once(self) -> None:
        connection = MagicMock()
        pull = daily_sync.NightWindowPull(
            target_date="2026-09-03",
            source_rows=1,
            mapped_rows=1,
            inserted_rows=1,
            skipped_existing=0,
            quarantine={},
            calc_adj_fallback_rows=0,
            validation={"warning_count": 0},
            forward_map={},
        )
        funds = {"000001": {"type": "混合型", "name": "样本"}}
        gate_result = {
            "target_rows": 1,
            "coverage": 1.0,
            "lag_distribution": {"T0": 1},
        }
        with tempfile.TemporaryDirectory() as directory:
            plan = Path(directory) / "state.json"
            with (
                patch.object(daily_sync, "_pull_night_window", return_value=pull),
                patch.object(daily_sync, "load_funds", return_value=funds),
                patch.object(daily_sync, "_existing_codes", return_value=set()),
                patch.object(
                    daily_sync, "_remaining_codes", return_value=[]
                ) as remaining,
                patch.object(
                    daily_sync, "_fallback_and_write", return_value=(1, [])
                ) as fallback,
                patch.object(
                    daily_sync, "integrity_gate", return_value=gate_result
                ) as gate,
            ):
                report = daily_sync.run_finalize(
                    connection,
                    today="2026-09-04",
                    plan_path=plan,
                    eastmoney_concurrency=2,
                    akshare_enabled=False,
                )

        fallback.assert_called_once_with(
            connection,
            ["000001"],
            "2026-09-03",
            "2026-09-03",
            ANY,
            eastmoney_concurrency=2,
            akshare_enabled=False,
        )
        remaining.assert_called_once_with(connection, "2026-09-03", ["000001"])
        gate.assert_called_once_with(connection, "2026-09-03")
        self.assertEqual(report["mode"], "full_finalize")
        self.assertEqual(report["fallback_inserted"], 1)
        self.assertEqual(report["integrity_gate"], gate_result)
        self.assertEqual(report["source_health"]["eastmoney_attempted"], 0)

    def test_reconcile_fetches_five_nav_date_windows(self) -> None:
        connection = MagicMock()
        dates = ["2026-09-03", "2026-09-02", "2026-09-01", "2026-08-31", "2026-08-28"]
        ordered_dates = list(reversed(dates))
        source_pages = [[tushare_row(day=day.replace("-", ""))] for day in dates]
        funds = {"000001": {"type": "混合型", "name": "样本"}}
        present = {"000001"}
        # Tushare 入库后 09-01 仍缺键；东财补漏后的最终复核已补齐。
        existing_code_snapshots = (
            [present, present, set(), present, present]
            + [present, present, set(), present, present]
            + [present] * 5
        )
        with (
            patch.object(daily_sync, "trade_dates", return_value=dates),
            patch.object(
                daily_sync, "fetch_tushare_date", side_effect=source_pages
            ) as fetch,
            patch.object(daily_sync, "load_funds", return_value=funds),
            patch.object(
                daily_sync,
                "load_mapping",
                return_value=(
                    {"000001.OF": "000001"},
                    {"000001": {"ts_code": "000001.OF", "status": "L"}},
                ),
            ),
            patch.object(daily_sync, "load_existing_rows", return_value={}),
            patch.object(
                daily_sync, "insert_rows_and_advance_watermarks", return_value=1
            ) as write,
            patch.object(
                daily_sync, "_existing_codes", side_effect=existing_code_snapshots
            ),
            patch.object(
                daily_sync, "_fallback_and_write", return_value=(1, [])
            ) as fallback,
            patch.object(daily_sync, "integrity_gate", return_value={}),
        ):
            report = daily_sync.run_reconcile(
                connection,
                today="2026-09-04",
                eastmoney_concurrency=2,
                akshare_enabled=False,
            )

        self.assertEqual(fetch.call_count, 5)
        self.assertEqual(write.call_count, 5)
        fallback.assert_called_once_with(
            connection,
            ["000001"],
            "2026-08-28",
            "2026-09-03",
            ANY,
            eastmoney_concurrency=2,
            akshare_enabled=False,
            required_dates=ordered_dates,
        )
        self.assertEqual(report["reconcile_dates"], ordered_dates)
        self.assertEqual(report["tushare_missing_keys_before_insert"], 5)
        self.assertEqual(report["continuity_gaps_before"], 1)
        self.assertEqual(report["continuity_gaps_after"], 0)

    def test_per_fund_worker_is_eastmoney_only(self) -> None:
        expected = [{"trade_date": "2026-09-03", "nav": 1.02}]
        with patch.object(
            worker.eastmoney, "fetch_nav_incremental", return_value=expected
        ) as eastmoney:
            rows = worker._fetch_incremental("000001", "2026-09-02", "2026-09-03")

        self.assertEqual(rows, expected)
        eastmoney.assert_called_once_with("000001", "2026-09-02", "2026-09-03")
        self.assertFalse(hasattr(worker, "howbuy"))
        self.assertFalse(hasattr(worker, "tushare_client"))


if __name__ == "__main__":
    import unittest

    unittest.main()
