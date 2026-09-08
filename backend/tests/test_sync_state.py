"""Phase 2 基金净值同步水位测试；全部数据库 I/O 均为 mock。"""

# pylint: disable=duplicate-code,missing-function-docstring,protected-access
from __future__ import annotations

import datetime as dt
from unittest import TestCase
from unittest.mock import patch

from app.fund_nav import sync_state
from app.fund_nav.fetch import worker
from scripts import init_sync_state


class SyncStateTests(TestCase):
    """验证水位批量读写与缺表兼容。"""

    def test_get_watermarks_loads_all_rows_with_one_query(self) -> None:
        rows = [
            {"fund_code": "000001", "watermark_date": dt.date(2026, 7, 30)},
            {"fund_code": "000002", "watermark_date": "2026-07-31"},
            {"fund_code": "000003", "watermark_date": None},
        ]
        with patch.object(sync_state.database, "select", return_value=rows) as select:
            result = sync_state.get_watermarks("nav")

        self.assertEqual(
            result,
            {
                "000001": "2026-07-30",
                "000002": "2026-07-31",
            },
        )
        select.assert_called_once_with(
            "fund_sync_state",
            [
                ("task_kind", "eq.nav"),
                ("select", "fund_code,watermark_date"),
                ("limit", 1_000_000),
            ],
        )

    def test_set_watermark_uses_idempotent_batch_upsert(self) -> None:
        with patch.object(sync_state.database, "batch_insert") as batch_insert:
            sync_state.set_watermark("000001", "nav", dt.date(2026, 7, 31))

        batch_insert.assert_called_once_with(
            "fund_sync_state",
            [
                {
                    "fund_code": "000001",
                    "task_kind": "nav",
                    "watermark_date": "2026-07-31",
                    "status": "success",
                    "attempts": 0,
                    "last_error": None,
                }
            ],
        )

    def test_worker_falls_back_to_stored_latest_without_preloaded_watermark(
        self,
    ) -> None:
        with (
            patch.object(worker.no_nav_blacklist, "is_blacklisted", return_value=False),
            patch.object(
                worker.calendar_crud, "base_trade_date", return_value="2026-07-31"
            ),
            patch.object(
                worker.nav_crud, "stored_latest", return_value="2026-07-31"
            ) as stored,
        ):
            result = worker._process_one("000001", None)

        self.assertEqual(result, "skip")
        stored.assert_called_once_with("000001", "fund_nav")


class InitSyncStateTests(TestCase):
    """验证初始化回填统计和 dry-run 不写入。"""

    def test_run_init_counts_backfill_and_respects_dry_run(self) -> None:
        source = {"000001": "2026-07-30", "000002": "2026-07-31"}
        with (
            patch.object(
                init_sync_state, "load_source_watermarks", return_value=source
            ) as load,
            patch.object(
                init_sync_state.sync_state, "set_watermarks", return_value=2
            ) as write,
        ):
            dry_result = init_sync_state.run_init(dry_run=True, limit=2)
            live_result = init_sync_state.run_init(dry_run=False, limit=2)

        self.assertEqual(
            dry_result,
            {
                "candidates": 2,
                "backfilled": 0,
                "dry_run": True,
            },
        )
        self.assertEqual(
            live_result,
            {
                "candidates": 2,
                "backfilled": 2,
                "dry_run": False,
            },
        )
        self.assertEqual(load.call_count, 2)
        load.assert_called_with(2)
        write.assert_called_once_with("nav", source)


if __name__ == "__main__":
    import unittest

    unittest.main()
