"""净值数据治理 M1-M6 的核心回归测试。"""
from __future__ import annotations

import argparse
import datetime as dt
import math
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from app import db as database
from app.fund_nav.crud import nav_crud
from app.fund_nav.fetch import adj_engine, backfill_worker
from app.fund_nav.fetch.tushare_client import rows_from_response
from cli.__main__ import build_parser


class NavGovernancePureTests(TestCase):
    """不触碰生产数据库的复权、解析、限速和参数测试。"""

    def test_tushare_response_is_mapped_to_dict_rows(self) -> None:
        response = {"code": 0, "data": {
            "fields": ["ts_code", "nav_date", "unit_nav", "adj_nav"],
            "items": [["519981.OF", "20260827", 2.613, 4.209546]],
        }}
        self.assertEqual(rows_from_response(response), [{
            "ts_code": "519981.OF",
            "nav_date": "20260827",
            "unit_nav": 2.613,
            "adj_nav": 4.209546,
        }])

    def test_front_adjustment_uses_events_before_ex_date(self) -> None:
        nav_rows = [
            {"trade_date": "2026-01-01", "nav": 1.20},
            {"trade_date": "2026-01-02", "nav": 1.10},
            {"trade_date": "2026-01-03", "nav": 1.15},
        ]
        events = [{
            "event_type": "div",
            "ex_date": "2026-01-02",
            "cash_per_unit": 0.10,
        }]

        result = adj_engine.calculate_adjusted_rows(nav_rows, events)

        self.assertAlmostEqual(result[0]["adj_nav"], 1.20 * (1.10 + 0.10) / 1.10)
        self.assertAlmostEqual(result[1]["adj_nav"], 1.10)
        self.assertAlmostEqual(result[2]["adj_nav"], 1.15)

    def test_front_adjustment_combines_split_ratio(self) -> None:
        nav_rows = [
            {"trade_date": "2026-01-01", "nav": 1.0},
            {"trade_date": "2026-01-02", "nav": 1.0},
        ]
        events = [{
            "event_type": "split",
            "ex_date": "2026-01-02",
            "split_ratio": 2.0,
        }]

        result = adj_engine.calculate_adjusted_rows(nav_rows, events)

        self.assertEqual(result[0]["adj_nav"], 2.0)
        self.assertEqual(result[1]["adj_nav"], 1.0)

    def test_relative_error_handles_zero_reference(self) -> None:
        self.assertEqual(adj_engine.relative_error(0, 0), 0.0)
        self.assertEqual(adj_engine.relative_error(0, 0.01), math.inf)
        self.assertAlmostEqual(adj_engine.relative_error(4.0, 4.01), 0.0025)

    def test_rate_limiter_waits_300ms_between_funds(self) -> None:
        clock = iter([10.0, 10.0, 10.1, 10.3])
        with (
            patch.object(backfill_worker.time, "monotonic", side_effect=lambda: next(clock)),
            patch.object(backfill_worker.time, "sleep") as sleep,
        ):
            backfill_worker._rate_limiter.reset()
            backfill_worker.wait_for_slot(0.3)
            backfill_worker.wait_for_slot(0.3)

        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 0.2)

    def test_cli_parses_governance_commands(self) -> None:
        parser = build_parser()
        args = parser.parse_args([
            "nav", "backfill", "--codes", "519981,000001", "--concurrency", "4",
            "--limit", "2",
        ])
        self.assertEqual(args.group, "nav")
        self.assertEqual(args.cmd, "backfill")
        self.assertEqual(args.codes, "519981,000001")
        self.assertEqual(args.concurrency, 4)
        self.assertEqual(args.limit, 2)

        args = parser.parse_args(["nav", "adj", "--src", "both", "--all"])
        self.assertEqual(args.src, "both")
        self.assertTrue(args.all)

    def test_backfill_process_one_is_idempotent_for_repeated_history(self) -> None:
        rows = [{
            "trade_date": "2026-08-27",
            "nav": 2.613,
            "acc_nav": 3.13,
            "daily_return": 0.1,
        }]
        with (
            patch.object(backfill_worker.eastmoney, "fetch_nav_full", return_value=rows),
            patch.object(backfill_worker.nav_crud, "upsert_nav_rows", return_value=1) as upsert,
            patch.object(backfill_worker.repair_crud, "mark_done_for_fund"),
            patch.object(backfill_worker, "wait_for_slot"),
        ):
            first = backfill_worker.backfill_one("519981")
            second = backfill_worker.backfill_one("519981")

        self.assertEqual(first["rows"], 1)
        self.assertEqual(second["rows"], 1)
        self.assertEqual(upsert.call_count, 2)
        self.assertEqual(upsert.call_args.args[0][0]["fund_code"], "519981")

    def test_nav_crud_merges_adj_columns_without_overwriting_nav(self) -> None:
        existing = [{
            "fund_code": "519981", "trade_date": "2026-08-27", "nav": 2.613,
            "acc_nav": 3.13, "daily_return": 0.1, "adj_nav": 4.209546,
            "adj_src": "tushare", "fetch_time": "old",
        }]
        with (
            patch.object(database, "select", return_value=existing),
            patch.object(database, "batch_insert") as batch,
        ):
            nav_crud.upsert_nav_rows([{
                "fund_code": "519981", "trade_date": "2026-08-27", "nav": 2.614,
                "acc_nav": 3.131, "daily_return": 0.2, "fetch_time": "new",
            }])

        row = batch.call_args.args[1][0]
        self.assertEqual(row["nav"], 2.614)
        self.assertEqual(row["adj_nav"], 4.209546)
        self.assertEqual(row["adj_src"], "tushare")


class NavGovernanceDatabaseTests(TestCase):
    """使用统一 DB mock 验证 CRUD 的状态转换。"""

    def test_repair_queue_pending_filter_is_index_friendly(self) -> None:
        with patch.object(database, "select", return_value=[]) as select:
            from app.fund_nav.crud import repair_crud

            repair_crud.list_retryable(limit=20, now="2026-08-30T21:30:00")

        params = select.call_args.args[1]
        self.assertIn(("status", "in.(pending,failed)"), params)
        self.assertIn(("next_retry_at", "lte.2026-08-30T21:30:00"), params)


class SchemaGovernanceTests(TestCase):
    """schema 文本必须覆盖新增治理对象。"""

    def test_schema_declares_new_columns_tables_and_indexes(self) -> None:
        schema = Path(__file__).resolve().parents[1].joinpath("schema_sqlite.sql").read_text()
        for needle in (
            "adj_nav FLOAT",
            "adj_src TEXT",
            "CREATE TABLE IF NOT EXISTS fund_div_split",
            "CREATE TABLE IF NOT EXISTS nav_repair_queue",
            "ix_nav_repair_queue_status_retry",
        ):
            self.assertIn(needle, schema)


if __name__ == "__main__":
    import unittest

    unittest.main()
