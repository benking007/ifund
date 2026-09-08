"""净值 cron 时段与调度入口测试；不访问网络或生产库。"""

from __future__ import annotations

# pylint: disable=missing-function-docstring
import datetime as dt
import tempfile
from contextlib import nullcontext
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

from scripts import daily_nav_sync


class DailyNavScheduleTests(TestCase):
    """固定 47 轮夜间边界，并确保 08:00 对账路由不变。"""

    def test_night_poll_has_47_five_minute_slots(self) -> None:
        slots = daily_nav_sync.night_poll_slots()

        self.assertEqual(len(slots), 47)
        self.assertEqual(slots[0], dt.time(20, 0))
        self.assertEqual(slots[-1], dt.time(23, 50))
        self.assertIn(dt.time(20, 55), slots)
        self.assertIn(dt.time(21, 55), slots)
        self.assertIn(dt.time(22, 55), slots)
        self.assertNotIn(dt.time(23, 55), slots)

    def test_night_round_switches_to_finalize_only_at_2350(self) -> None:
        day = dt.date(2026, 9, 4)

        self.assertEqual(
            daily_nav_sync.resolve_round(
                "night", dt.datetime.combine(day, dt.time(20, 0))
            ),
            "incremental",
        )
        self.assertEqual(
            daily_nav_sync.resolve_round(
                "night", dt.datetime.combine(day, dt.time(23, 45))
            ),
            "incremental",
        )
        self.assertEqual(
            daily_nav_sync.resolve_round(
                "night", dt.datetime.combine(day, dt.time(23, 50))
            ),
            "finalize",
        )

    def test_reconcile_route_and_legacy_aliases_are_unchanged(self) -> None:
        at_eight = dt.datetime(
            2026, 9, 5, 8, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))
        )

        self.assertEqual(daily_nav_sync.resolve_round("auto", at_eight), "reconcile")
        self.assertEqual(daily_nav_sync.resolve_round("morning", at_eight), "reconcile")
        self.assertEqual(daily_nav_sync.resolve_round("noon", at_eight), "reconcile")

    def test_trade_day_runs_incremental(self) -> None:
        connection = MagicMock()
        report = {
            "target_date": "2026-09-04",
            "tushare_mapped": 10,
            "tushare_inserted": 2,
            "target_changed": False,
            "noop_streak": 0,
            "source_health": {"tushare_calls": 1},
        }
        with (
            patch.object(daily_nav_sync, "configure_logging"),
            patch.object(
                daily_nav_sync, "internal_lock", return_value=nullcontext(True)
            ),
            patch.object(
                daily_nav_sync.daily_sync,
                "connect_mysql",
                return_value=connection,
            ),
            patch.object(
                daily_nav_sync.daily_sync, "is_trade_date", return_value=True
            ) as is_trade_date,
            patch.object(
                daily_nav_sync.daily_sync,
                "run_incremental",
                return_value=report,
            ) as incremental,
        ):
            exit_code = daily_nav_sync.main(
                ["--round", "incremental", "--today", "2026-09-04"]
            )

        self.assertEqual(exit_code, 0)
        is_trade_date.assert_called_once_with(connection, "2026-09-04")
        incremental.assert_called_once()
        connection.close.assert_called_once_with()

    def test_holiday_skips_incremental_without_evidence(self) -> None:
        connection = MagicMock()
        with (
            self.assertLogs(daily_nav_sync.logger, level="INFO") as captured,
            patch.object(daily_nav_sync, "configure_logging"),
            patch.object(
                daily_nav_sync, "internal_lock", return_value=nullcontext(True)
            ),
            patch.object(
                daily_nav_sync.daily_sync,
                "connect_mysql",
                return_value=connection,
            ),
            patch.object(
                daily_nav_sync.daily_sync, "is_trade_date", return_value=False
            ),
            patch.object(daily_nav_sync.daily_sync, "run_incremental") as incremental,
            patch.object(daily_nav_sync.daily_sync, "atomic_json") as write_json,
        ):
            exit_code = daily_nav_sync.main(
                ["--round", "incremental", "--today", "2026-10-01"]
            )

        self.assertEqual(exit_code, 0)
        self.assertIn(
            "非交易日跳过（2026-10-01 节假日/休市）", "\n".join(captured.output)
        )
        incremental.assert_not_called()
        write_json.assert_not_called()
        connection.close.assert_called_once_with()

    def test_holiday_skips_finalize_without_evidence(self) -> None:
        connection = MagicMock()
        with (
            patch.object(daily_nav_sync, "configure_logging"),
            patch.object(
                daily_nav_sync, "internal_lock", return_value=nullcontext(True)
            ),
            patch.object(
                daily_nav_sync.daily_sync,
                "connect_mysql",
                return_value=connection,
            ),
            patch.object(
                daily_nav_sync.daily_sync, "is_trade_date", return_value=False
            ) as is_trade_date,
            patch.object(daily_nav_sync.daily_sync, "run_finalize") as finalize,
            patch.object(daily_nav_sync.daily_sync, "atomic_json") as write_json,
        ):
            exit_code = daily_nav_sync.main(
                ["--round", "finalize", "--today", "2026-10-01"]
            )

        self.assertEqual(exit_code, 0)
        is_trade_date.assert_called_once_with(connection, "2026-10-01")
        finalize.assert_not_called()
        write_json.assert_not_called()
        connection.close.assert_called_once_with()

    def test_reconcile_does_not_use_trade_day_guard(self) -> None:
        connection = MagicMock()
        report = {
            "target_date": "2026-09-04",
            "integrity_gate": {"target_rows": 10, "coverage": 1.0},
            "source_health": {"tushare_calls": 6, "eastmoney_success": 1},
        }
        with tempfile.TemporaryDirectory() as directory:
            evidence_dir = Path(directory)
            with (
                patch.object(daily_nav_sync, "configure_logging"),
                patch.object(
                    daily_nav_sync, "internal_lock", return_value=nullcontext(True)
                ),
                patch.object(
                    daily_nav_sync.daily_sync,
                    "connect_mysql",
                    return_value=connection,
                ),
                patch.object(daily_nav_sync.daily_sync, "is_trade_date") as guard,
                patch.object(
                    daily_nav_sync.daily_sync,
                    "run_reconcile",
                    return_value=report,
                ) as reconcile,
                patch.object(daily_nav_sync.daily_sync, "atomic_json"),
                patch.object(
                    daily_nav_sync.nav_alerts,
                    "evaluate",
                    return_value={"alerts": []},
                ),
                patch.object(
                    daily_nav_sync.nav_alerts,
                    "emit",
                    return_value={"latest": "latest.json", "event": None},
                ),
            ):
                exit_code = daily_nav_sync.main(
                    [
                        "--round",
                        "reconcile",
                        "--today",
                        "2026-09-05",
                        "--evidence-dir",
                        str(evidence_dir),
                    ]
                )

        self.assertEqual(exit_code, 0)
        guard.assert_not_called()
        reconcile.assert_called_once()
        connection.close.assert_called_once_with()

    def test_finalize_main_writes_one_run_evidence_file(self) -> None:
        connection = MagicMock()
        report = {
            "target_date": "2026-09-04",
            "integrity_gate": {"target_rows": 10, "coverage": 1.0},
            "source_health": {"tushare_calls": 6, "eastmoney_success": 1},
        }
        with tempfile.TemporaryDirectory() as directory:
            evidence_dir = Path(directory)
            with (
                patch.object(daily_nav_sync, "configure_logging"),
                patch.object(
                    daily_nav_sync, "internal_lock", return_value=nullcontext(True)
                ),
                patch.object(
                    daily_nav_sync.daily_sync,
                    "connect_mysql",
                    return_value=connection,
                ),
                patch.object(
                    daily_nav_sync.daily_sync, "is_trade_date", return_value=True
                ),
                patch.object(
                    daily_nav_sync.daily_sync,
                    "run_finalize",
                    return_value=report,
                ) as finalize,
                patch.object(daily_nav_sync.daily_sync, "atomic_json") as write_json,
                patch.object(
                    daily_nav_sync.nav_alerts,
                    "evaluate",
                    return_value={"alerts": []},
                ),
                patch.object(
                    daily_nav_sync.nav_alerts,
                    "emit",
                    return_value={"latest": "latest.json", "event": None},
                ),
            ):
                exit_code = daily_nav_sync.main(
                    ["--round", "finalize", "--evidence-dir", str(evidence_dir)]
                )

        self.assertEqual(exit_code, 0)
        finalize.assert_called_once()
        evidence_path, evidence_payload = write_json.call_args.args
        self.assertEqual(evidence_path.parent, evidence_dir.resolve())
        self.assertIn("daily-nav-finalize-", evidence_path.name)
        self.assertEqual(evidence_payload, report)
        connection.close.assert_called_once_with()

    def test_integrity_alert_returns_nonzero_after_evidence_write(self) -> None:
        connection = MagicMock()
        report = {
            "target_date": "2026-09-04",
            "integrity_gate": {"target_rows": 10, "coverage": 0.79},
            "source_health": {"tushare_calls": 1, "eastmoney_success": 0},
        }
        alert_payload = {"alerts": [{"code": "nav_coverage_below_baseline"}]}
        with (
            patch.object(daily_nav_sync, "configure_logging"),
            patch.object(
                daily_nav_sync, "internal_lock", return_value=nullcontext(True)
            ),
            patch.object(
                daily_nav_sync.daily_sync, "connect_mysql", return_value=connection
            ),
            patch.object(daily_nav_sync.daily_sync, "is_trade_date", return_value=True),
            patch.object(
                daily_nav_sync.daily_sync, "run_finalize", return_value=report
            ),
            patch.object(daily_nav_sync.daily_sync, "atomic_json") as write_json,
            patch.object(
                daily_nav_sync.nav_alerts, "evaluate", return_value=alert_payload
            ),
            patch.object(
                daily_nav_sync.nav_alerts,
                "emit",
                return_value={"latest": "latest.json", "event": "event.json"},
            ),
        ):
            exit_code = daily_nav_sync.main(
                ["--round", "finalize", "--today", "2026-09-04"]
            )

        self.assertEqual(exit_code, 1)
        self.assertTrue(write_json.called)
        self.assertEqual(report["monitoring"]["alerts"], alert_payload["alerts"])


if __name__ == "__main__":
    import unittest

    unittest.main()
