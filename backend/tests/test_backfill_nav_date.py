"""指定日期历史净值补拉脚本的 mock 测试。"""

from __future__ import annotations

# pylint: disable=protected-access
from unittest import TestCase
from unittest.mock import patch

from scripts import backfill_nav_date

TARGET_DATE = "2026-08-31"


def _source_row(day: str = TARGET_DATE) -> dict:
    return {
        "trade_date": day,
        "nav": 1.2345,
        "acc_nav": 1.3456,
        "daily_return": 0.12,
        "cum_return": None,
    }


class BackfillNavDateTests(TestCase):
    """验证集合筛选、日期窗口、幂等和 dry-run。"""

    def test_target_selection_only_contains_funds_missing_date(self) -> None:
        """目标集合只含缺日期基金，且筛选固定为两次查询。"""
        with patch.object(
            backfill_nav_date.database,
            "select",
            side_effect=[
                [{"code": "000001"}, {"code": "000002"}, {"code": "000003"}],
                [{"fund_code": "000002"}],
            ],
        ) as select:
            funds, targets = backfill_nav_date.load_target_codes(TARGET_DATE)

        self.assertEqual(funds, ["000001", "000002", "000003"])
        self.assertEqual(targets, ["000001", "000003"])
        self.assertEqual(select.call_count, 2)
        self.assertEqual(select.call_args_list[0].args[0], "funds")
        self.assertEqual(
            select.call_args_list[1].args,
            (
                "fund_nav",
                [
                    ("trade_date", "eq.2026-08-31"),
                    ("select", "distinct fund_code"),
                ],
            ),
        )

    def test_incremental_fetch_uses_expected_start_and_end(self) -> None:
        """最新日晚于目标日时，从目标日前五天拉到目标日。"""
        with (
            patch.object(
                backfill_nav_date,
                "load_target_codes",
                return_value=(["000001"], ["000001"]),
            ),
            patch.object(
                backfill_nav_date.nav_worker.no_nav_blacklist,
                "blacklisted_codes",
                return_value=set(),
            ),
            patch.object(
                backfill_nav_date.nav_crud,
                "stored_latest",
                return_value="2026-09-01",
            ),
            patch.object(
                backfill_nav_date.eastmoney,
                "fetch_nav_incremental",
                return_value=[
                    _source_row("2026-08-25"),
                    _source_row("2026-08-28"),
                    _source_row(),
                    _source_row("2026-09-01"),
                ],
            ) as fetch,
            patch.object(backfill_nav_date.nav_crud, "insert_rows") as insert_rows,
        ):
            result = backfill_nav_date.run_backfill(TARGET_DATE, workers=1)

        fetch.assert_called_once_with("000001", "2026-08-26", "2026-08-31")
        insert_rows.assert_called_once()
        self.assertEqual(insert_rows.call_args.args[0], "fund_nav")
        self.assertEqual(
            [row["trade_date"] for row in insert_rows.call_args.args[1]],
            ["2026-08-28", "2026-08-31"],
        )
        self.assertEqual(result["fetched"], 1)
        self.assertEqual(result["backfilled"], 1)
        self.assertEqual(result["failed"], 0)

    def test_existing_target_date_is_idempotently_skipped(self) -> None:
        """已有目标日期时不进入逐基金查询、抓取或写入。"""
        with (
            patch.object(
                backfill_nav_date,
                "load_target_codes",
                return_value=(["000001", "000002"], []),
            ),
            patch.object(backfill_nav_date.nav_crud, "stored_latest") as stored_latest,
            patch.object(backfill_nav_date.eastmoney, "fetch_nav_incremental") as fetch,
            patch.object(backfill_nav_date.nav_crud, "insert_rows") as insert_rows,
        ):
            result = backfill_nav_date.run_backfill(TARGET_DATE)

        self.assertEqual(result["total"], 2)
        self.assertEqual(result["target"], 0)
        self.assertEqual(result["skipped"], 2)
        stored_latest.assert_not_called()
        fetch.assert_not_called()
        insert_rows.assert_not_called()

    def test_dry_run_fetches_and_reports_without_any_write(self) -> None:
        """dry-run 保留抓取统计，但不写净值或无净值黑名单。"""
        with (
            patch.object(
                backfill_nav_date,
                "load_target_codes",
                return_value=(["000001", "000002"], ["000001"]),
            ),
            patch.object(
                backfill_nav_date.nav_worker.no_nav_blacklist,
                "blacklisted_codes",
                return_value=set(),
            ),
            patch.object(
                backfill_nav_date.nav_worker.no_nav_blacklist,
                "record",
            ) as record,
            patch.object(
                backfill_nav_date.nav_crud,
                "stored_latest",
                return_value="2026-09-01",
            ),
            patch.object(
                backfill_nav_date.eastmoney,
                "fetch_nav_incremental",
                return_value=[_source_row()],
            ),
            patch.object(backfill_nav_date.nav_crud, "insert_rows") as insert_rows,
        ):
            result = backfill_nav_date.run_backfill(
                TARGET_DATE,
                dry_run=True,
                workers=1,
            )

        self.assertEqual(
            result,
            {
                "total": 2,
                "target": 1,
                "fetched": 1,
                "backfilled": 0,
                "skipped": 1,
                "no_nav": 0,
                "failed": 0,
                "dry_run": True,
                "failures": [],
            },
        )
        insert_rows.assert_not_called()
        record.assert_not_called()

    def test_cli_supports_required_controls_and_default_workers(self) -> None:
        """CLI 接受日期、dry-run、JSON、limit，并默认 8 个线程。"""
        args = backfill_nav_date.build_parser().parse_args(
            ["--date", TARGET_DATE, "--dry-run", "--json", "--limit", "10"]
        )

        self.assertEqual(args.date, TARGET_DATE)
        self.assertTrue(args.dry_run)
        self.assertTrue(args.json)
        self.assertEqual(args.limit, 10)
        self.assertEqual(args.workers, 8)


if __name__ == "__main__":
    import unittest

    unittest.main()
