"""AkShare rank 全量快照、日期过滤、幂等回填和 dry-run 测试。"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import pandas as pd

from app import db as database
from app.fund_nav.fetch import rank_backfill
from cli import nav as nav_cli
from cli.__main__ import build_parser


def _frame(*rows: tuple) -> pd.DataFrame:
    return pd.DataFrame(
        rows,
        columns=[
            "基金代码",
            "日期",
            "单位净值",
            "累计净值",
            "日增长率",
        ],
    )


def _snapshot(rows: list[dict]) -> dict:
    return {
        "rows": rows,
        "api_calls": 1,
        "source_rows": len(rows),
        "failed": 0,
        "failures": [],
        "fetch_time": "2026-09-02T12:00:00",
    }


class RankSnapshotFetchTests(TestCase):
    """全市场快照单次调用 AkShare 并映射到 fund_nav。"""

    def test_collects_full_snapshot_once_and_maps_actual_akshare_columns(self) -> None:
        """一次接收全量 DataFrame，并映射 AkShare 当前中文列名。"""
        frame = _frame(
            ("000001", dt.date(2026, 9, 1), 1.2345, 2.3456, 0.12),
            ("000002", "2026-08-31", "0.9876", "1.2345", "-0.20"),
            ("000003", "2026-09-01", 2.0, None, None),
        )
        original_requests = rank_backfill._fund_rank_em.requests  # pylint: disable=protected-access
        with patch.object(
            rank_backfill.ak,
            "fund_open_fund_rank_em",
            return_value=frame,
        ) as fetch:
            result = rank_backfill.collect_rank_snapshot()

        fetch.assert_called_once_with(symbol="全部")
        self.assertIs(rank_backfill._fund_rank_em.requests, original_requests)  # pylint: disable=protected-access
        self.assertEqual(result["api_calls"], 1)
        self.assertEqual(result["source_rows"], 3)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(
            result["rows"][0],
            {
                "fund_code": "000001",
                "trade_date": "2026-09-01",
                "nav": 1.2345,
                "acc_nav": 2.3456,
                "daily_return": 0.12,
                "fetch_time": result["fetch_time"],
            },
        )
        self.assertEqual(result["rows"][1]["trade_date"], "2026-08-31")
        self.assertIsNone(result["rows"][2]["acc_nav"])

    def test_collect_preserves_default_request_params_and_adds_timeout(self) -> None:
        """保留 AkShare 默认 pi/pn，不注入分页或日期，只附加连接/读取超时。"""
        requests_module = MagicMock()
        frame = _frame(("000001", "2026-09-01", 1.0, 1.1, 0.1))
        fund_rank_module = rank_backfill._fund_rank_em  # pylint: disable=protected-access

        def fetch_full_snapshot(*, symbol):
            self.assertEqual(symbol, "全部")
            fund_rank_module.requests.get(
                "https://fund.eastmoney.com/data/rankhandler.aspx",
                params={"pi": "1", "pn": "30000", "op": "ph"},
            )
            return frame

        with (
            patch.object(fund_rank_module, "requests", requests_module),
            patch.object(
                rank_backfill.ak,
                "fund_open_fund_rank_em",
                side_effect=fetch_full_snapshot,
            ) as fetch,
        ):
            result = rank_backfill.collect_rank_snapshot()

        fetch.assert_called_once_with(symbol="全部")
        self.assertEqual(result["api_calls"], 1)
        requests_module.Session.return_value.get.assert_called_once_with(
            "https://fund.eastmoney.com/data/rankhandler.aspx",
            params={"pi": "1", "pn": "30000", "op": "ph"},
            timeout=(5, 15),
        )

    def test_fetch_uses_one_spawn_subprocess(self) -> None:
        """主进程只向一个 spawn worker 提交一次全量采集。"""
        context = object()
        future = MagicMock()
        future.result.return_value = {"rows": []}
        executor = MagicMock()
        executor.__enter__.return_value.submit.return_value = future

        with (
            patch.object(
                rank_backfill.multiprocessing,
                "get_context",
                return_value=context,
            ) as get_context,
            patch.object(
                rank_backfill,
                "ProcessPoolExecutor",
                return_value=executor,
            ) as pool,
        ):
            result = rank_backfill.fetch_rank_snapshot()

        get_context.assert_called_once_with("spawn")
        pool.assert_called_once_with(max_workers=1, mp_context=context)
        executor.__enter__.return_value.submit.assert_called_once_with(
            rank_backfill.collect_rank_snapshot,
        )
        self.assertEqual(result, {"rows": []})

    def test_missing_columns_log_raw_preview_before_raising(self) -> None:
        """上游改列名时先记录原始列和样本，再显式失败。"""
        frame = pd.DataFrame([{"基金代码": "000001", "意外列": "bad"}])

        with (
            self.assertLogs(rank_backfill.logger, level="ERROR") as captured,
            self.assertRaises(rank_backfill.RankSnapshotParseError),
        ):
            rank_backfill.parse_rank_frame(
                frame,
                fetch_time="2026-09-02T12:00:00",
            )

        self.assertIn("原始返回", "\n".join(captured.output))
        self.assertIn("意外列", "\n".join(captured.output))


class RankBackfillDatabaseTests(TestCase):
    """只写快照日期晚于本地最新日期的 funds 基金。"""

    @staticmethod
    def _rows() -> list[dict]:
        return [
            {
                "fund_code": "000001",
                "trade_date": "2026-09-01",
                "nav": 1.1,
                "acc_nav": 1.2,
                "daily_return": 0.1,
                "fetch_time": "2026-09-02T12:00:00",
            },
            {
                "fund_code": "000002",
                "trade_date": "2026-09-01",
                "nav": 2.1,
                "acc_nav": 2.2,
                "daily_return": 0.2,
                "fetch_time": "2026-09-02T12:00:00",
            },
            {
                "fund_code": "000003",
                "trade_date": "2026-08-31",
                "nav": 3.1,
                "acc_nav": 3.2,
                "daily_return": 0.3,
                "fetch_time": "2026-09-02T12:00:00",
            },
        ]

    def test_only_missing_0831_and_0901_are_upserted_and_rerun_skips(self) -> None:
        """只补晚于本地最新日的 8/31、9/1 行，重跑无新增写入。"""
        funds = [{"code": code} for code in ("000001", "000002", "000003", "000004")]
        latest = {
            "000001": "2026-08-31",
            "000002": "2026-09-01",
            "000003": "2026-08-28",
        }

        def select(table, params):
            if table == "funds":
                return funds
            if table == "fund_nav":
                day = params["trade_date"]
                return [
                    {"fund_code": code}
                    for code, stored_day in latest.items()
                    if stored_day >= day
                ]
            return []

        with (
            patch.object(database, "select", side_effect=select),
            patch.object(
                rank_backfill,
                "fetch_rank_snapshot",
                return_value=_snapshot(self._rows()),
            ),
            patch.object(database, "batch_insert") as batch_insert,
        ):
            result = rank_backfill.run_rank_backfill()

        self.assertEqual(result["total"], 4)
        self.assertEqual(result["eligible"], 2)
        self.assertEqual(result["backfilled"], 2)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["no_nav"], 1)
        self.assertEqual(result["failed"], 0)
        written = batch_insert.call_args.args[1]
        self.assertEqual([row["fund_code"] for row in written], ["000001", "000003"])
        self.assertEqual(
            [row["trade_date"] for row in written], ["2026-09-01", "2026-08-31"]
        )

        rerun_latest = {
            "000001": "2026-09-01",
            "000002": "2026-09-01",
            "000003": "2026-08-31",
        }

        def select_rerun(table, params):
            if table == "funds":
                return funds
            if table == "fund_nav":
                day = params["trade_date"]
                return [
                    {"fund_code": code}
                    for code, stored_day in rerun_latest.items()
                    if stored_day >= day
                ]
            return []

        with (
            patch.object(database, "select", side_effect=select_rerun),
            patch.object(
                rank_backfill,
                "fetch_rank_snapshot",
                return_value=_snapshot(self._rows()),
            ),
            patch.object(database, "batch_insert") as second_insert,
        ):
            rerun = rank_backfill.run_rank_backfill()

        self.assertEqual(rerun["backfilled"], 0)
        self.assertEqual(rerun["skipped"], 3)
        second_insert.assert_not_called()

    def test_dry_run_reports_candidates_without_writing(self) -> None:
        """dry-run 报告待补、跳过和无净值数量，且不触发写库。"""
        funds = [{"code": code} for code in ("000001", "000002", "000004")]
        latest = {"000001": "2026-08-31", "000002": "2026-09-01"}

        def select(table, params):
            if table == "funds":
                return funds
            if table == "fund_nav":
                day = params["trade_date"]
                return [
                    {"fund_code": code}
                    for code, stored_day in latest.items()
                    if stored_day >= day
                ]
            return []

        with (
            patch.object(database, "select", side_effect=select),
            patch.object(
                rank_backfill,
                "fetch_rank_snapshot",
                return_value=_snapshot(self._rows()[:2]),
            ),
            patch.object(database, "batch_insert") as batch_insert,
        ):
            result = rank_backfill.run_rank_backfill(dry_run=True)

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["eligible"], 1)
        self.assertEqual(result["would_backfill"], 1)
        self.assertEqual(result["backfilled"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["no_nav"], 1)
        self.assertEqual(result["rows"], 0)
        batch_insert.assert_not_called()

    def test_target_date_filters_mismatched_latest_snapshot_rows(self) -> None:
        """target-date 只过滤返回快照，不下推历史日期且明确报告不匹配。"""
        funds = [{"code": code} for code in ("000001", "000002", "000003")]

        def select(table, params):
            if table == "funds":
                return funds
            return []

        with (
            patch.object(database, "select", side_effect=select),
            patch.object(
                rank_backfill,
                "fetch_rank_snapshot",
                return_value=_snapshot(self._rows()),
            ) as fetch,
            patch.object(database, "batch_insert") as batch_insert,
            self.assertLogs(rank_backfill.logger, level="WARNING") as captured,
        ):
            result = rank_backfill.run_rank_backfill(
                dry_run=True,
                target_date="2026-09-01",
            )

        fetch.assert_called_once_with()
        self.assertEqual(result["snapshot_dates"], ["2026-08-31", "2026-09-01"])
        self.assertEqual(result["date_filtered"], 1)
        self.assertEqual(result["eligible"], 2)
        self.assertEqual(result["would_backfill"], 2)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["no_nav"], 0)
        self.assertIn("F10 lsjz", result["warnings"][0])
        self.assertIn("已过滤 1 行", "\n".join(captured.output))
        batch_insert.assert_not_called()


class RankBackfillCliTests(TestCase):
    """CLI 参数、JSON 统计与失败退出码。"""

    def test_cli_parses_rankbackfill_controls(self) -> None:
        """CLI 接受 dry-run、日期和基金数量，不再暴露分页参数。"""
        args = build_parser().parse_args(
            [
                "nav",
                "rankbackfill",
                "--dry-run",
                "--json",
                "--limit",
                "10",
                "--target-date",
                "2026-09-01",
            ]
        )

        self.assertEqual(args.group, "nav")
        self.assertEqual(args.cmd, "rankbackfill")
        self.assertTrue(args.dry_run)
        self.assertTrue(args.json)
        self.assertEqual(args.limit, 10)
        self.assertEqual(args.target_date, "2026-09-01")
        self.assertFalse(hasattr(args, "page_size"))
        self.assertFalse(hasattr(args, "max_pages"))

    def test_cli_returns_nonzero_when_stats_contain_failure(self) -> None:
        """统计含失败时 CLI 使用退出码 1。"""
        args = SimpleNamespace(
            dry_run=True,
            limit=1,
            target_date=None,
            json=True,
        )
        failed = rank_backfill._empty_stats(dry_run=True, total=1)  # pylint: disable=protected-access
        failed["failed"] = 1
        with (
            patch.object(rank_backfill, "run_rank_backfill", return_value=failed),
            patch.object(nav_cli.output, "emit"),
            self.assertRaises(SystemExit) as raised,
        ):
            nav_cli.cmd_rankbackfill(args)

        self.assertEqual(raised.exception.code, 1)
