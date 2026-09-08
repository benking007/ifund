"""Tushare 基金公司、基础扩展与份额字段映射测试。"""
from __future__ import annotations

from unittest import TestCase
from unittest.mock import patch

from app.fund_reference import tushare_sync


class FundReferenceMappingTests(TestCase):
    """覆盖权威映射、单位语义、分页与四类样本。"""

    def test_basic_maps_only_authoritative_ts_code(self) -> None:
        raw = {
            "ts_code": "510300.SH",
            "name": "沪深300ETF",
            "management": "华泰柏瑞基金管理有限公司",
            "market": "E",
            "status": "L",
            "m_fee": "0.50",
            "found_date": "20120504",
        }
        mapped = tushare_sync.map_basic_row(
            raw,
            {"510300.SH": "510300"},
            {tushare_sync.company_key(raw["management"]): 7},
        )
        self.assertIsNotNone(mapped)
        self.assertEqual(mapped["fund_code"], "510300")
        self.assertEqual(mapped["company_id"], 7)
        self.assertEqual(mapped["management_fee"], 0.5)
        self.assertEqual(mapped["found_date"], "2012-05-04")
        self.assertIsNone(tushare_sync.map_basic_row(raw, {}, {}))

    def test_share_records_upstream_unit_and_source_field(self) -> None:
        mapped = tushare_sync.map_share_row(
            {
                "ts_code": "510300.SH",
                "trade_date": "20260903",
                "fd_share": "865421.5",
            },
            {"510300.SH": "510300"},
        )
        self.assertEqual(mapped["trade_date"], "2026-09-03")
        self.assertEqual(mapped["share_value"], 865421.5)
        self.assertEqual(mapped["share_unit"], "10k_shares")
        self.assertEqual(mapped["source_field"], "fd_share")

    def test_share_history_uses_offset_pagination(self) -> None:
        page = [{"ts_code": "510300.SH"}] * 2
        with patch.object(
            tushare_sync.tushare_client,
            "call",
            side_effect=[page, [{"ts_code": "510300.SH"}]],
        ) as call:
            rows = tushare_sync.fetch_share_history("510300.SH", page_size=2)
        self.assertEqual(len(rows), 3)
        self.assertEqual(call.call_args_list[0].args[1]["offset"], 0)
        self.assertEqual(call.call_args_list[1].args[1]["offset"], 2)

    def test_basic_samples_cover_four_groups(self) -> None:
        rows = [
            {
                "ts_code": "510300.SH",
                "name": "沪深300ETF",
                "market": "E",
                "status": "L",
            },
            {
                "ts_code": "160706.SZ",
                "name": "嘉实300LOF",
                "market": "E",
                "status": "L",
            },
            {
                "ts_code": "000001.OF",
                "name": "场外股票",
                "market": "O",
                "status": "L",
            },
            {
                "ts_code": "500001.SH",
                "name": "退市基金",
                "market": "E",
                "status": "D",
            },
        ]
        selected = tushare_sync.select_basic_samples(rows, per_group=1)
        self.assertEqual(
            {row["ts_code"] for row in selected},
            {row["ts_code"] for row in rows},
        )

