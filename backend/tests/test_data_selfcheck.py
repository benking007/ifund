"""data_selfcheck 命令行行为测试（数据库全 mock，不访问生产）。"""

from __future__ import annotations

import datetime as dt
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from scripts import data_selfcheck


class DataSelfcheckCliTest(unittest.TestCase):
    """验证 JSON 纯输出、动态参数传递与退出码。"""

    @patch.object(data_selfcheck, "run_checks")
    def test_json_with_issues_is_single_object_and_returns_one(
        self, run_checks
    ) -> None:
        """发现数据问题时仍只输出 JSON，并返回 1。"""
        run_checks.return_value = {"ok": False, "issues": ["历史问题"], "rows": 1}
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            exit_code = data_selfcheck.main(
                [
                    "--json",
                    "--as-of",
                    "2026-09-02",
                    "--window-days",
                    "30",
                    "--stale-days",
                    "9",
                ]
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(stdout.getvalue()), run_checks.return_value)
        run_checks.assert_called_once_with(dt.date(2026, 9, 2), 30, 9)

    @patch.object(data_selfcheck, "run_checks")
    def test_json_without_issues_returns_zero(self, run_checks) -> None:
        """没有数据问题时返回 0。"""
        run_checks.return_value = {"ok": True, "issues": []}
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            exit_code = data_selfcheck.main(["--json", "--as-of", "2026-09-02"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), run_checks.return_value)
        run_checks.assert_called_once_with(
            dt.date(2026, 9, 2),
            data_selfcheck.DEFAULT_WINDOW_DAYS,
            data_selfcheck.DEFAULT_STALE_DAYS,
        )

    @patch.object(data_selfcheck, "run_checks", side_effect=RuntimeError("连接失败"))
    def test_json_execution_error_is_pure_json_and_returns_two(
        self, _run_checks
    ) -> None:
        """执行异常也保持 JSON 纯输出，并返回 2。"""
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = data_selfcheck.main(["--json", "--as-of", "2026-09-02"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["issues"], ["自检执行失败"])
        self.assertIn("RuntimeError: 连接失败", payload["error"])
        self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
