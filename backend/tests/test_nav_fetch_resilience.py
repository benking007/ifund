"""Static timeout coverage and persistent no-NAV blacklist tests."""

from __future__ import annotations

import ast
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from app.common.network import TimeoutRequestsProxy, install_module_timeout
from app.fund_holdings.fetch import worker as holdings_worker
from app.fund_nav.fetch import backfill_worker, no_nav_blacklist, tushare_client
from app.fund_nav.fetch import worker as nav_worker
from app.fund_nav.fetch.errors import F10_404, RANK_SNAPSHOT_MISSING, NoNavDataError


class NetworkTimeoutCoverageTests(TestCase):
    """Every direct requests/urlopen call must carry an explicit timeout."""

    def test_direct_http_calls_all_have_explicit_timeout(self) -> None:
        app_dir = Path(__file__).resolve().parents[1] / "app"
        missing = []
        request_methods = {"get", "post", "put", "patch", "delete", "request"}
        for path in app_dir.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(
                    node.func, ast.Attribute
                ):
                    continue
                is_requests_call = (
                    isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "requests"
                    and node.func.attr in request_methods
                )
                is_urlopen = node.func.attr == "urlopen"
                if (is_requests_call or is_urlopen) and not any(
                    keyword.arg == "timeout" for keyword in node.keywords
                ):
                    missing.append(f"{path.relative_to(app_dir.parent)}:{node.lineno}")
        self.assertEqual(missing, [])

    def test_akshare_request_proxies_inject_connect_and_read_timeout(self) -> None:
        for proxy_class in (nav_worker._RequestsProxy, holdings_worker._RequestsProxy):
            with self.subTest(proxy=proxy_class.__module__):
                requests_module = MagicMock()
                session = requests_module.Session.return_value
                proxy_class(requests_module).get("https://example.invalid")

                session.get.assert_called_once_with(
                    "https://example.invalid",
                    timeout=(5, 15),
                )

    def test_tushare_client_posts_with_connect_and_read_timeout(self) -> None:
        response = MagicMock()
        response.json.return_value = {"code": 0, "data": {"fields": [], "items": []}}
        limiter = MagicMock()
        with (
            patch.object(tushare_client, "get_token", return_value="test-token"),
            patch.object(tushare_client, "_get_limiter", return_value=limiter),
            patch.object(
                tushare_client.requests, "post", return_value=response
            ) as post,
        ):
            rows = tushare_client.call("fund_nav")

        self.assertEqual(rows, [])
        limiter.wait.assert_called_once_with()
        self.assertEqual(post.call_args.kwargs["timeout"], (5, 15))

    def test_generic_akshare_proxy_covers_get_and_post_idempotently(self) -> None:
        requests_module = MagicMock()
        module = SimpleNamespace(requests=requests_module)

        install_module_timeout(module)
        proxy = module.requests
        install_module_timeout(module)

        self.assertIs(module.requests, proxy)
        self.assertIsInstance(proxy, TimeoutRequestsProxy)
        proxy.get("https://example.invalid/get")
        proxy.post("https://example.invalid/post")
        session = requests_module.Session.return_value
        session.get.assert_called_once_with(
            "https://example.invalid/get",
            timeout=(5, 15),
        )
        session.post.assert_called_once_with(
            "https://example.invalid/post",
            timeout=(5, 15),
        )


class BackfillRetryTests(TestCase):
    """No-NAV business outcomes bypass retry and the repair queue."""

    def test_f10_404_is_skipped_without_single_source_blacklist(self) -> None:
        error = NoNavDataError("404", reason=F10_404)
        with (
            patch.object(
                backfill_worker.no_nav_blacklist, "is_blacklisted", return_value=False
            ),
            patch.object(backfill_worker, "wait_for_slot"),
            patch.object(
                backfill_worker.eastmoney, "fetch_nav_full", side_effect=error
            ) as fetch,
            patch.object(backfill_worker.no_nav_blacklist, "record") as record,
            patch.object(backfill_worker.repair_crud, "record_failure") as repair,
            patch.object(backfill_worker.time, "sleep") as sleep,
        ):
            result = backfill_worker.backfill_one("000012")

        self.assertEqual(result["status"], "skip")
        fetch.assert_called_once_with("000012")
        record.assert_not_called()
        repair.assert_not_called()
        sleep.assert_not_called()


class NoNavBlacklistTests(TestCase):
    """The independent SQLite state survives reopen and remains queryable."""

    def test_blacklist_persists_and_rank_missing_reason_is_queryable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nav_blacklist.db"
            first = no_nav_blacklist.record(
                "000012",
                F10_404,
                sources=["tushare", "eastmoney"],
                detected_at="2026-09-02T12:00:00",
                expires_at="2099-10-02T12:00:00",
                path=path,
            )
            no_nav_blacklist.record_rank_snapshot_missing(
                "006922", confirming_sources=["eastmoney"], path=path
            )

            self.assertEqual(
                first,
                {
                    "fund_code": "000012",
                    "reason": F10_404,
                    "detected_at": "2026-09-02T12:00:00",
                    "source": "eastmoney,tushare",
                    "expires_at": "2099-10-02T12:00:00",
                    "evidence_count": 2,
                },
            )
            self.assertTrue(no_nav_blacklist.is_blacklisted("000012", path=path))
            self.assertEqual(no_nav_blacklist.get_record("000012", path=path), first)
            records = {
                row["fund_code"]: row
                for row in no_nav_blacklist.list_records(path=path)
            }
            self.assertEqual(records["006922"]["reason"], RANK_SNAPSHOT_MISSING)
            self.assertEqual(
                no_nav_blacklist.blacklisted_codes(path=path), {"000012", "006922"}
            )

    def test_single_source_blacklist_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nav_blacklist.db"
            with self.assertRaisesRegex(ValueError, "at least two sources"):
                no_nav_blacklist.record(
                    "000012", F10_404, sources=["eastmoney"], path=path
                )
