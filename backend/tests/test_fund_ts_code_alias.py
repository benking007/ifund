"""Tushare 同六位码渠道 alias 构建与幂等写入测试。"""

from __future__ import annotations

import json
from unittest import TestCase
from unittest.mock import patch

from app.fund_nav import ts_code_map


class FundTsCodeAliasTests(TestCase):
    """alias 是 additive 证据层，不改变主映射选择。"""

    def test_build_aliases_keeps_exchange_primary_and_otc_alias(self) -> None:
        mappings = [
            {
                "fund_code": "158008",
                "ts_code": "158008.SZ",
                "market": "E",
                "source": "fund_basic",
                "verified_at": "2026-09-04T10:00:00",
            },
            {
                "fund_code": "501001",
                "ts_code": "501001.SH",
                "market": "E",
                "source": "fund_basic",
                "verified_at": "2026-09-04T10:00:00",
            },
        ]
        evidence = [
            {
                "entity_kind": "ambiguous",
                "code": "158008",
                "reason": "multiple_ts_codes",
                "detail_json": "158008.OF,158008.SZ",
                "created_at": "2026-09-04T10:00:00",
            },
            {
                "entity_kind": "ambiguous",
                "code": "501001",
                "reason": "multiple_ts_codes",
                "detail_json": "501001.OF,501001.SH",
                "created_at": "2026-09-04T10:00:00",
            },
        ]

        rows = ts_code_map.build_alias_rows(mappings, evidence)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["primary_ts_code"], "158008.SZ")
        self.assertEqual(rows[0]["alias_ts_code"], "158008.OF")
        self.assertEqual(rows[0]["primary_channel"], "SZ")
        self.assertEqual(rows[0]["alias_channel"], "OF")
        self.assertEqual(rows[1]["primary_channel"], "SH")
        self.assertEqual(
            json.loads(rows[0]["source_evidence"])["authority"], "tushare.fund_basic"
        )

    def test_persist_aliases_is_stable_on_repeated_input(self) -> None:
        rows = [
            {
                "fund_code": "158008",
                "primary_ts_code": "158008.SZ",
                "alias_ts_code": "158008.OF",
                "primary_channel": "SZ",
                "alias_channel": "OF",
                "source": "tushare.fund_basic",
                "source_evidence": "{}",
                "status": "active",
                "verified_at": "2026-09-04T10:00:00",
            }
        ]
        with (
            patch.object(ts_code_map, "ensure_schema"),
            patch.object(ts_code_map.database, "batch_insert") as insert,
        ):
            first = ts_code_map.persist_aliases(rows)
            second = ts_code_map.persist_aliases(rows)

        self.assertEqual((first, second), (1, 1))
        self.assertEqual(insert.call_count, 2)
        self.assertEqual(insert.call_args_list[0].args, insert.call_args_list[1].args)
