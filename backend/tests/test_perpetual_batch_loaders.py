"""Regression tests for the perpetual pipeline's batched data loading."""

from __future__ import annotations

from datetime import date, timedelta
from unittest import TestCase
from unittest.mock import patch

from app.perpetual.algo import loader
from app.perpetual.api import router


class PerpetualBatchLoaderTests(TestCase):
    def test_load_nav_series_batch_groups_rows_by_code(self) -> None:
        rows = [
            {"fund_code": "000001", "trade_date": "2024-01-02", "nav": 1.02},
            {"fund_code": "000001", "trade_date": "2024-01-03", "nav": None},
            {"fund_code": "000002", "trade_date": "2024-01-02", "nav": 0.98},
        ]

        with patch.object(loader.database, "select", return_value=rows) as select:
            result = loader.load_nav_series_batch(["000001", "000002"], "2024-01-03")

        self.assertEqual(result, {
            "000001": [("2024-01-02", 1.02)],
            "000002": [("2024-01-02", 0.98)],
        })
        self.assertEqual(select.call_count, 1)
        self.assertEqual(select.call_args.args[0], "fund_nav")

    def test_current_tenure_days_batch_applies_as_of_adjustment(self) -> None:
        as_of = (date.today() - timedelta(days=10)).isoformat()
        rows = [
            {"fund_code": "000001", "tenure_days": 1000},
            {"fund_code": "000002", "tenure_days": None},
        ]

        with patch.object(loader.database, "select", return_value=rows):
            result = loader.current_tenure_days_batch(["000001", "000002"], as_of)

        self.assertEqual(result, {"000001": 990, "000002": 0})

    def test_load_quarter_holdings_batch_applies_disclosure_cutoff(self) -> None:
        rows = [
            {
                "fund_code": "000001", "quarter": "2025Q1",
                "asset_code": "A", "hold_ratio": 0.2,
            },
            {
                "fund_code": "000001", "quarter": "2025Q3",
                "asset_code": "B", "hold_ratio": 0.3,
            },
        ]

        with patch.object(loader.database, "select", return_value=rows):
            result = loader.load_quarter_holdings_batch(["000001"], "2025-08-20")

        self.assertEqual(result, {"000001": {"2025Q1": {"A": 0.2}}})

    def test_build_result_uses_one_batch_call_per_dataset(self) -> None:
        universe = [{"code": "000001"}, {"code": "000002"}]
        with (
            patch.object(router.loader, "load_universe", return_value=universe),
            patch.object(router.loader, "load_nav_series_batch", return_value={}) as nav,
            patch.object(router.loader, "current_tenure_days_batch", return_value={}) as tenure,
            patch.object(router.loader, "load_quarter_holdings_batch", return_value={}) as holdings,
            patch.object(router.pipeline, "run", return_value={"ok": True}),
        ):
            result = router.build_result(as_of="2025-08-20")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(nav.call_args.args, (["000001", "000002"], "2025-08-20"))
        self.assertEqual(tenure.call_args.args, (["000001", "000002"], "2025-08-20"))
        self.assertEqual(holdings.call_args.args, (["000001", "000002"], "2025-08-20"))


if __name__ == "__main__":
    import unittest

    unittest.main()
