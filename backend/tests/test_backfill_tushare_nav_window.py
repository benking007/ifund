"""Tushare 单日全市场净值窗口回补脚本测试。"""

from __future__ import annotations

import argparse
from unittest import TestCase
from unittest.mock import Mock, patch

from scripts import (  # pylint: disable=import-error
    backfill_tushare_nav_window as backfill,
)


def _row(code: str, *, day: str = "20260903", unit_nav: float | None = 1.2) -> dict:
    return {
        "ts_code": code,
        "ann_date": day,
        "nav_date": day,
        "unit_nav": unit_nav,
        "accum_nav": 1.3,
        "adj_nav": 1.4,
    }


class BackfillTushareNavWindowTests(TestCase):
    """验证分页、保守映射和 INSERT IGNORE。"""

    def test_fetch_date_pages_uses_limit_and_offset(self) -> None:
        """满页后递增 offset，短页终止。"""
        pages = [
            [_row("000001.OF"), _row("000002.OF")],
            [_row("000003.OF")],
        ]
        budget = backfill.ApiBudget(3)
        with patch.object(backfill.tushare_client, "call", side_effect=pages) as call:
            rows, page_count = backfill.fetch_date_pages("2026-09-03", 2, budget)

        self.assertEqual(len(rows), 3)
        self.assertEqual(page_count, 2)
        self.assertEqual(budget.used, 2)
        self.assertEqual(call.call_args_list[0].args[1]["offset"], 0)
        self.assertEqual(call.call_args_list[1].args[1]["offset"], 2)

    def test_mapping_quarantines_unmapped_mismatch_and_empty_nav(self) -> None:
        """不确定映射和空单位净值不写事实表。"""
        rows = [
            _row("000001.OF"),
            _row("999999.OF"),
            _row("000003.OF", unit_nav=None),
            _row("000004.OF"),
        ]
        mapped, quarantine = backfill.map_source_rows(
            "2026-09-03",
            rows,
            {
                "000001.OF": "000001",
                "000003.OF": "000003",
                "000004.OF": "123456",
            },
            {"000001.OF", "000002.OF", "000003.OF", "000004.OF"},
        )

        self.assertEqual([row["fund_code"] for row in mapped], ["000001"])
        self.assertEqual(quarantine["unmapped_source_codes"], ["999999.OF"])
        self.assertEqual(quarantine["missing_unit_nav"], ["000003.OF"])
        self.assertEqual(
            quarantine["mapped_code_mismatch"],
            [{"ts_code": "000004.OF", "fund_code": "123456"}],
        )
        self.assertEqual(quarantine["mapped_without_tushare"], ["000002.OF"])
        self.assertEqual(mapped[0]["adj_src"], "tushare")

    def test_insert_missing_is_insert_ignore_without_update_clause(self) -> None:
        """写 SQL 只能忽略冲突，不能包含更新分支。"""
        cursor = Mock()
        cursor.rowcount = 1
        inserted = backfill.insert_missing(
            cursor,
            [
                {
                    "fund_code": "000001",
                    "trade_date": "2026-09-03",
                    "nav": 1.2,
                    "acc_nav": 1.3,
                    "daily_return": None,
                    "adj_nav": 1.4,
                    "adj_src": "tushare",
                    "fetch_time": "2026-09-04T12:00:00+08:00",
                }
            ],
        )

        sql = cursor.executemany.call_args.args[0]
        self.assertEqual(inserted, 1)
        self.assertIn("INSERT IGNORE INTO fund_nav", sql)
        self.assertNotIn("UPDATE", sql.upper())

    def test_api_budget_stops_before_exceeding_cap(self) -> None:
        """请求预算在发出超额请求前失败。"""
        budget = backfill.ApiBudget(1)
        budget.reserve()
        with self.assertRaisesRegex(RuntimeError, "预算耗尽"):
            budget.reserve()

    def test_float_comparison_accepts_mysql_single_precision_rounding(self) -> None:
        """Tushare 六位小数写入 MySQL FLOAT 后的舍入不应误报。"""
        self.assertTrue(backfill._float_equal(1.15222, 1.152218))  # pylint: disable=protected-access
        self.assertFalse(backfill._float_equal(1.1523, 1.152218))  # pylint: disable=protected-access

    def test_candidates_can_be_restored_from_prior_report(self) -> None:
        """修正验证容差时复用报告缓存，不额外调用 Tushare。"""
        candidates = backfill.candidates_from_report(
            {
                "verification_samples": [
                    {
                        "fund_code": "000001",
                        "trade_date": "2026-09-03",
                        "source": {"ts_code": "000001.OF"},
                        "expected": {
                            "nav": 1.27,
                            "acc_nav": 3.843,
                            "adj_nav": 8.259623,
                            "adj_src": "tushare",
                        },
                    }
                ]
            }
        )
        self.assertEqual(candidates[0]["fund_code"], "000001")
        self.assertEqual(candidates[0]["adj_nav"], 8.259623)

    def test_parser_requires_evidence_paths(self) -> None:
        """证据文件路径必须显式提供。"""
        args = backfill.build_parser().parse_args(
            [
                "--state-path",
                "/tmp/state.json",
                "--report-path",
                "/tmp/report.json",
                "--quarantine-path",
                "/tmp/quarantine.json",
            ]
        )
        self.assertIsInstance(args, argparse.Namespace)
        self.assertEqual(args.page_size, 5000)
        self.assertEqual(args.max_api_calls, 50)
