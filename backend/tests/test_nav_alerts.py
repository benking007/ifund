"""净值完整性告警判据与统一文件出口测试。"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock

from app.fund_nav import alerts

LIMITS = {
    "coverage_ratio": 0.8,
    "no_update_trade_days": 2,
    "blacklist_daily_new": 50,
    "repair_max_age_hours": 24,
    "watermark_fact_mismatch": 0,
}


class NavAlertTests(TestCase):
    """五项阈值必须可独立触发，边界本身不误报。"""

    def test_all_governance_conditions_trigger(self) -> None:
        report = {
            "round": "finalize",
            "target_date": "2026-09-04",
            "integrity_gate": {"coverage": 0.79, "baseline_median": 100},
        }
        observations = {
            "consecutive_trade_days_without_update": 2,
            "blacklist_daily_new": 51,
            "repair_open_count": 1,
            "repair_oldest_age_hours": 25,
            "watermark_fact_mismatch": 1,
        }

        payload = alerts.build_payload(
            report, observations, configured_thresholds=LIMITS
        )

        self.assertEqual(payload["status"], "alert")
        self.assertEqual(
            {item["code"] for item in payload["alerts"]},
            {
                "nav_coverage_below_baseline",
                "nav_no_update_two_trade_days",
                "nav_blacklist_daily_spike",
                "nav_repair_queue_overage",
                "nav_watermark_fact_mismatch",
            },
        )

    def test_non_alert_boundaries_remain_ok(self) -> None:
        report = {
            "round": "reconcile",
            "target_date": "2026-09-04",
            "integrity_gate": {"coverage": 0.8, "baseline_median": 100},
        }
        observations = {
            "consecutive_trade_days_without_update": 1,
            "blacklist_daily_new": 50,
            "repair_open_count": 1,
            "repair_oldest_age_hours": 24,
            "watermark_fact_mismatch": 0,
        }

        payload = alerts.build_payload(
            report, observations, configured_thresholds=LIMITS
        )

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["alerts"], [])

    def test_watermark_fact_check_uses_batched_business_keys(self) -> None:
        cursor = MagicMock()
        cursor.fetchall.side_effect = [
            [
                {"fund_code": "000001", "watermark_date": "2026-09-03"},
                {"fund_code": "000002", "watermark_date": "2026-09-03"},
            ],
            [{"fund_code": "000001", "trade_date": "2026-09-03"}],
        ]

        mismatch = alerts._watermark_fact_mismatch(cursor, batch_size=2)

        self.assertEqual(mismatch, 1)
        self.assertIn(
            "(fund_code,trade_date) IN", cursor.execute.call_args_list[1].args[0]
        )

    def test_emit_writes_latest_and_event_json(self) -> None:
        payload = {
            "status": "alert",
            "round": "finalize",
            "target_date": "2026-09-04",
            "alerts": [{"code": "nav_repair_queue_overage"}],
        }
        with tempfile.TemporaryDirectory() as directory:
            result = alerts.emit(payload, alert_dir=Path(directory))
            latest = Path(result["latest"])
            event = Path(result["event"])

            self.assertTrue(latest.exists())
            self.assertTrue(event.exists())
            self.assertEqual(json.loads(latest.read_text())["status"], "alert")
