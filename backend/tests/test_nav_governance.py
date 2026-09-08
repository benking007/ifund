"""净值数据治理 M1-M6 的核心回归测试。"""
from __future__ import annotations

import math
from types import SimpleNamespace
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from app import db as database
from app.fund_nav.crud import nav_crud, repair_crud
from app.fund_nav.fetch import adj_engine, backfill_worker
from app.fund_nav.fetch.tushare_client import rows_from_response
from cli import nav as nav_cli
from cli.__main__ import build_parser


class NavGovernancePureTests(TestCase):
    """不触碰生产数据库的复权、解析、限速和参数测试。"""

    def test_tushare_response_is_mapped_to_dict_rows(self) -> None:
        """把 Tushare fields/items 转换为字典行。"""
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

    def test_given_519981_tushare_sample_keeps_front_adjusted_value(self) -> None:
        """保留 Tushare 返回的前复权净值。"""
        source_rows = [{
            "nav_date": "20260827",
            "unit_nav": "2.6130",
            "accum_nav": "3.1300",
            "adj_nav": "4.209546",
        }]
        with (
            patch.object(adj_engine.tushare_client, "fetch_fund_nav", return_value=source_rows),
            patch.object(adj_engine.nav_crud, "upsert_adj_rows", return_value=1) as upsert,
        ):
            result = adj_engine.fetch_adj_tushare(["519981"])

        self.assertEqual(result["rows"], 1)
        self.assertEqual(upsert.call_args.args[0], "519981")
        self.assertEqual(upsert.call_args.args[1][0]["trade_date"], "2026-08-27")
        self.assertEqual(upsert.call_args.args[1][0]["adj_nav"], 4.209546)

    def test_tushare_failure_is_recorded_in_repair_queue(self) -> None:
        """Tushare 失败时写入 adj 修复队列。"""
        with (
            patch.object(
                adj_engine.tushare_client,
                "fetch_fund_nav",
                side_effect=RuntimeError("upstream timeout"),
            ),
            patch.object(adj_engine.repair_crud, "record_failure") as record_failure,
        ):
            result = adj_engine.fetch_adj_tushare(["519981"])

        self.assertEqual(result["failed"], ["519981"])
        record_failure.assert_called_once_with(
            "519981", "adj", "upstream timeout", attempts=1,
        )

    def test_cross_check_uses_event_table_independently(self) -> None:
        """交叉检查从独立事件表计算本地复权值。"""
        nav_rows = [
            {"trade_date": "2026-08-26", "nav": 2.50, "adj_nav": 2.6041667, "adj_src": "tushare"},
            {"trade_date": "2026-08-27", "nav": 2.40, "adj_nav": 2.40, "adj_src": "tushare"},
            {"trade_date": "2026-08-28", "nav": 2.50, "adj_nav": 2.50, "adj_src": "tushare"},
        ]
        events = [{
            "event_type": "div", "ex_date": "2026-08-27", "cash_per_unit": 0.10,
        }]
        with (
            patch.object(adj_engine.nav_crud, "list_nav_rows", return_value=nav_rows),
            patch.object(adj_engine.div_split_crud, "list_events", return_value=events),
        ):
            result = adj_engine.cross_check("519981")

        self.assertEqual(result["sampled"], 3)
        self.assertLess(result["max_relative_error"], 0.005)
        self.assertFalse(result["warning"])

    def test_cross_check_counts_tushare_calc_mismatch_separately(self) -> None:
        """分别统计 Tushare 与自算值不一致的样本。"""
        nav_rows = [
            {"trade_date": "2026-08-26", "nav": 2.50, "adj_nav": 2.90, "adj_src": "tushare"},
            {"trade_date": "2026-08-27", "nav": 2.40, "adj_nav": 2.40, "adj_src": "tushare"},
            {"trade_date": "2026-08-28", "nav": 2.50, "adj_nav": 2.50, "adj_src": "tushare"},
        ]
        events = [{
            "event_type": "div", "ex_date": "2026-08-27", "cash_per_unit": 0.10,
        }]
        with (
            patch.object(adj_engine.nav_crud, "list_nav_rows", return_value=nav_rows),
            patch.object(adj_engine.div_split_crud, "list_events", return_value=events),
        ):
            result = adj_engine.cross_check("519981")

        self.assertEqual(result["sampled"], 3)
        self.assertEqual(result["comparable_count"], 3)
        self.assertEqual(result["incomparable_count"], 0)
        self.assertEqual(result["mismatch_count"], 1)
        self.assertTrue(result["warning"])
        self.assertEqual(result["mismatch_samples"][0]["trade_date"], "2026-08-26")

    def test_cross_check_counts_missing_calc_sample_as_incomparable(self) -> None:
        """自算值缺失的样本应标记为不可比。"""
        nav_rows = [{
            "trade_date": "2026-08-26", "nav": 2.50, "adj_nav": 2.60, "adj_src": "tushare",
        }]
        with (
            patch.object(adj_engine.nav_crud, "list_nav_rows", return_value=nav_rows),
            patch.object(adj_engine.div_split_crud, "list_events", return_value=[]),
            patch.object(adj_engine, "calculate_adjusted_rows", return_value=[{
                "trade_date": "2026-08-26", "adj_nav": None,
            }]),
        ):
            result = adj_engine.cross_check("519981")

        self.assertEqual(result["comparable_count"], 0)
        self.assertEqual(result["incomparable_count"], 1)
        self.assertEqual(result["mismatch_count"], 0)
        self.assertTrue(result["warning"])
        self.assertEqual(result["incomparable_samples"][0]["reason"], "calc_missing")

    def test_calc_only_null_does_not_replace_existing_adjustment(self) -> None:
        """only-null 模式不覆盖已有复权值。"""
        nav_rows = [
            {
                "trade_date": "2026-08-26", "nav": 2.50,
                "adj_nav": 9.90, "adj_src": "tushare",
            },
            {
                "trade_date": "2026-08-27", "nav": 2.40,
                "adj_nav": None, "adj_src": None,
            },
        ]
        with (
            patch.object(adj_engine.events_worker, "get_event_scan_status", return_value="done"),
            patch.object(adj_engine.nav_crud, "list_nav_rows", return_value=nav_rows),
            patch.object(adj_engine.div_split_crud, "list_events", return_value=[]),
            patch.object(adj_engine.nav_crud, "update_adj_rows", return_value=1) as update,
        ):
            result = adj_engine.calc_adj_from_events("519981", only_null=True)

        self.assertTrue(result["only_null"])
        self.assertEqual(update.call_args.args[1], [{
            "trade_date": "2026-08-27", "nav": 2.40,
            "adj_nav": 2.40, "adj_src": "calc",
        }])

    def test_calc_skips_when_event_scan_is_not_ready(self) -> None:
        """事件扫描未完成时不执行自算写回。"""
        with (
            patch.object(adj_engine.events_worker, "get_event_scan_status", return_value=None),
            patch.object(adj_engine.nav_crud, "list_nav_rows") as list_rows,
            patch.object(adj_engine.nav_crud, "update_adj_rows") as update,
        ):
            result = adj_engine.calc_adj_from_events("519981")

        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "event_scan_not_ready")
        list_rows.assert_not_called()
        update.assert_not_called()

    def test_cli_does_not_count_cross_check_warning_as_success(self) -> None:
        """交叉检查告警不计为成功。"""
        args = SimpleNamespace(
            codes="519981", types=None, all=False, limit=1, concurrency=1,
            src="both", only_null=False, json=True,
        )
        with (
            patch.object(backfill_worker, "resolve_codes", return_value=["519981"]),
            patch.object(backfill_worker, "ensure_batch_allowed"),
            patch.object(nav_cli, "_adj_one", return_value={
                "code": "519981",
                "rows": 3,
                "cross_check": {"warning": True, "warning_count": 1},
                "validation_warning": True,
            }),
            patch.object(nav_cli.output, "emit") as emit,
        ):
            nav_cli.cmd_adj(args)

        result = emit.call_args.args[0]
        self.assertEqual(result["success"], 0)
        self.assertEqual(result["fail"], 1)
        self.assertEqual(result["warnings"], 1)

    def test_front_adjustment_uses_events_before_ex_date(self) -> None:
        """除权日前的净值应用事件因子。"""
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
        """拆分因子参与前复权计算。"""
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
        """相对误差正确处理零基准。"""
        self.assertEqual(adj_engine.relative_error(0, 0), 0.0)
        self.assertEqual(adj_engine.relative_error(0, 0.01), math.inf)
        self.assertAlmostEqual(adj_engine.relative_error(4.0, 4.01), 0.0025)

    def test_rate_limiter_waits_300ms_between_funds(self) -> None:
        """东财限速器为相邻请求预留时间间隔。"""
        clock = iter([10.0, 10.1])
        with (
            patch.object(backfill_worker.time, "monotonic", side_effect=lambda: next(clock)),
            patch.object(backfill_worker.time, "sleep") as sleep,
        ):
            backfill_worker.reset_rate_limiter()
            backfill_worker.wait_for_slot(0.3)
            backfill_worker.wait_for_slot(0.3)

        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 0.2)

    def test_cli_parses_governance_commands(self) -> None:
        """解析净值治理 CLI 参数。"""
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

        args = parser.parse_args(["nav", "adj", "--src", "calc", "--only-null"])
        self.assertTrue(args.only_null)

    def test_backfill_process_one_is_idempotent_for_repeated_history(self) -> None:
        """重复回补同一历史行保持幂等。"""
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
        """净值写入合并复权列而不覆盖已有复权值。"""
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
        """修复队列过滤条件包含状态和重试时间。"""
        with patch.object(database, "select", return_value=[]) as select:
            repair_crud.list_retryable(limit=20, now="2026-08-30T21:30:00")

        params = select.call_args.args[1]
        self.assertIn(("status", "in.(pending,failed)"), params)
        self.assertIn(("next_retry_at", "lte.2026-08-30T21:30:00"), params)


class SchemaGovernanceTests(TestCase):
    """schema 文本必须覆盖新增治理对象。"""

    def test_schema_declares_new_columns_tables_and_indexes(self) -> None:
        """schema 文本包含治理所需对象。"""
        schema = Path(__file__).resolve().parents[1].joinpath(
            "schema_sqlite.sql"
        ).read_text(encoding="utf-8")
        for needle in (
            "adj_nav FLOAT",
            "adj_src TEXT",
            "CREATE TABLE IF NOT EXISTS fund_div_split",
            "CREATE TABLE IF NOT EXISTS event_scan_status",
            "CREATE TABLE IF NOT EXISTS nav_repair_queue",
            "ix_nav_repair_queue_status_retry",
        ):
            self.assertIn(needle, schema)


if __name__ == "__main__":
    import unittest

    unittest.main()
