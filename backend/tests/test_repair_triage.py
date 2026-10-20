"""修复队列分类、last_error 校正、归档 DDL 与事件扫描接口名。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from app.fund_nav.fetch import events_worker, tushare_client
from app.fund_nav.fetch.tushare_client import TushareError
from app.fund_nav.repair_triage import (
    ARCHIVE_ADJ_EMPTY,
    RETRY_ADJ,
    RETRY_EVENTS,
    RETRY_NAV,
    REVIEW,
    classify_failure,
    failure_message,
    group_failures,
    plan_status_check_ddl,
    select_batch,
)


BACKEND = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    path = BACKEND / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RepairTriageTests(TestCase):
    """覆盖生产里看到的错误文本。"""

    def test_production_error_classes(self) -> None:
        samples = [
            ("东财服务异常，请稍后再试", None, RETRY_NAV),
            ("NameError: Data_netWorthTrend is not defined", None, RETRY_NAV),
            ("event scan failed: 请指定正确的接口名", "adj", RETRY_EVENTS),
            ("Tushare adj_nav 返回空或无有效行", "adj", ARCHIVE_ADJ_EMPTY),
            ("000001", "adj", RETRY_ADJ),
            ("000001", "nav", RETRY_NAV),
            ("000001;000002", "adj", RETRY_ADJ),
            ("上游证书过期", "nav", REVIEW),
        ]
        for text, kind, expected in samples:
            with self.subTest(text=text, kind=kind):
                self.assertEqual(classify_failure(text, kind), expected)

    def test_failure_message_never_stores_bare_code(self) -> None:
        self.assertEqual(
            failure_message({"failed": ["000111"], "errors": ["Tushare adj_nav 返回空或无有效行"]}),
            "Tushare adj_nav 返回空或无有效行",
        )
        self.assertEqual(failure_message({"failed": ["000111"]}), "治理任务失败")
        self.assertEqual(failure_message({"errors": ["000111"]}), "治理任务失败")
        self.assertEqual(failure_message({"errors": ["000111;000222"]}), "治理任务失败")

    def test_batch_limit_is_per_bucket_and_skips_review(self) -> None:
        grouped = group_failures([
            {"id": 1, "last_error": "服务异常，请稍后再试", "task_kind": "nav"},
            {"id": 2, "last_error": "服务异常，请稍后再试", "task_kind": "nav"},
            {"id": 3, "last_error": "event scan failed: 请指定正确的接口名", "task_kind": "adj"},
            {"id": 4, "last_error": "证书过期", "task_kind": "nav"},
        ])
        chosen = select_batch(grouped, "all_retryable", 1)
        self.assertEqual([row["id"] for row in chosen], [1, 3])
        with self.assertRaises(ValueError):
            select_batch(grouped, REVIEW, 10)

    def test_archive_ddl_drops_old_check_once(self) -> None:
        statements = plan_status_check_ddl([
            {"name": "nav_repair_queue_chk_1", "clause": "(`status` in (_utf8mb4'pending',_utf8mb4'failed'))"},
        ])
        self.assertEqual(len(statements), 2)
        self.assertIn("DROP CHECK `nav_repair_queue_chk_1`", statements[0])
        self.assertIn("archived", statements[1])
        self.assertEqual(
            plan_status_check_ddl([
                {"name": "chk_nav_repair_queue_status", "clause": "status in ('failed','archived')"},
            ]),
            [],
        )

    def test_event_rows_do_not_call_fund_split(self) -> None:
        def reject_split(*_args, **_kwargs):
            raise AssertionError("fund_split must not be called")

        def passthrough(_label, func, *args, **kwargs):
            return func(*args, **kwargs)

        dividend = [{"ts_code": "000001.OF", "ex_date": "20260904", "div_cash": 0.1}]
        with (
            patch.object(events_worker, "call_tushare_with_retry", side_effect=passthrough),
            patch.object(events_worker.tushare_client, "fetch_fund_div", return_value=dividend),
            patch.object(events_worker.tushare_client, "resolve_ts_code", return_value="000001.OF"),
            patch.object(events_worker.tushare_client, "fetch_fund_split", side_effect=reject_split),
        ):
            rows = events_worker.fetch_event_rows("000001")
        self.assertEqual(rows[0]["event_type"], "div")
        self.assertEqual(rows[0]["ex_date"], "2026-09-04")

    def test_fetch_fund_split_does_not_hit_network(self) -> None:
        with patch("app.fund_nav.fetch.tushare_client.requests.post") as post:
            with self.assertRaises(TushareError) as caught:
                tushare_client.fetch_fund_split("000001")
        post.assert_not_called()
        self.assertNotIn("请指定正确的接口名", str(caught.exception))


class RepairScriptTests(TestCase):
    """dry-run 不写库；执行路径把真实错误写回原任务。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.repair_pass = _load_script("nav_repair_pass")
        cls.retry = _load_script("retry_nav_repair_queue")

    def test_repair_pass_does_not_overwrite_last_error_with_code(self) -> None:
        task = {"id": 8, "fund_code": "000111", "task_kind": "adj", "attempts": 2}
        fetched = {"failed": ["000111"], "errors": ["Tushare adj_nav 返回空或无有效行"]}
        with (
            patch.object(self.repair_pass.repair_crud, "mark_running"),
            patch.object(self.repair_pass.adj_engine, "fetch_adj_tushare", return_value=fetched),
            patch.object(self.repair_pass.repair_crud, "mark_failed") as mark_failed,
        ):
            _task, ok, error = self.repair_pass._run_task(task)
        self.assertFalse(ok)
        self.assertNotEqual(error, "000111")
        self.assertEqual(mark_failed.call_args.args[1], "Tushare adj_nav 返回空或无有效行")

    def test_dry_run_report_has_no_apply_side_effect(self) -> None:
        rows = [
            {"id": 1, "fund_code": "110011", "task_kind": "nav", "last_error": "服务异常，请稍后再试"},
            {"id": 2, "fund_code": "110022", "task_kind": "adj", "last_error": "Tushare adj_nav 返回空或无有效行"},
        ]
        with patch.object(self.retry, "_apply_one", side_effect=AssertionError("dry-run wrote")):
            report = self.retry.build_report(rows, bucket="archive_adj_empty", limit=50, apply=False)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["counts"]["retry_nav"], 1)
        self.assertEqual(report["counts"]["archive_adj_empty"], 1)
        self.assertEqual(report["planned"][0]["id"], 2)

    def test_apply_archives_without_delete_and_keeps_real_adj_error(self) -> None:
        with (
            patch.object(self.retry.repair_crud, "mark_archived") as archived,
            patch.object(self.retry.repair_crud, "mark_failed") as mark_failed,
            patch.object(
                self.retry.adj_engine,
                "fetch_adj_tushare",
                return_value={"failed": ["000111"]},
            ),
        ):
            archived_result = self.retry._apply_one({
                "id": 9, "fund_code": "000001", "action": "archive_adj_empty",
            })
            adj_result = self.retry._apply_one({
                "id": 4, "fund_code": "000111", "action": "retry_adj",
            })
        archived.assert_called_once_with(9)
        self.assertEqual(archived_result["result"], "archived")
        self.assertEqual(mark_failed.call_args.args, (4, "治理任务失败"))
        self.assertEqual(adj_result["error"], "治理任务失败")
        self.assertNotEqual(adj_result["error"], "000111")
