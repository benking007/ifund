"""ETF 联接基金血缘：名称解析、匹配与 upsert 幂等。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.fund_etf_linkage.parser import match_feeder_to_etf, parse_feeder_name

# 三例验证用的迷你 ETF 宇宙（含同指数不同公司干扰项）
_SAMPLE_ETFS = [
    {"code": "588080", "name": "科创50ETF易方达"},
    {"code": "588000", "name": "科创50ETF华夏"},
    {"code": "588050", "name": "科创50ETF工银"},
    {"code": "588060", "name": "科创50ETF广发"},
    {"code": "588370", "name": "科创50增强ETF南方"},
]


class ParseFeederNameTests(unittest.TestCase):
    """名称解析：公司前缀 + 指数关键词 + 份额/发起式后缀。"""

    def test_yifangda_kechuang50_strips_share_class(self) -> None:
        parsed = parse_feeder_name("易方达上证科创50联接C")
        self.assertEqual(parsed.company, "易方达")
        self.assertIn("科创50", parsed.index_keys)
        self.assertIn("上证科创50", parsed.index_keys)

    def test_huaxia_kechuang50_strips_etf_and_share_class(self) -> None:
        parsed = parse_feeder_name("华夏科创50ETF联接A")
        self.assertEqual(parsed.company, "华夏")
        self.assertIn("科创50", parsed.index_keys)

    def test_gongyin_kechuang50_uses_short_company(self) -> None:
        parsed = parse_feeder_name("工银科创50ETF联接C")
        self.assertEqual(parsed.company, "工银")
        self.assertIn("科创50", parsed.index_keys)

    def test_initiator_suffix_is_stripped(self) -> None:
        parsed = parse_feeder_name("广发科创50ETF发起式联接A")
        self.assertEqual(parsed.company, "广发")
        self.assertIn("科创50", parsed.index_keys)


class MatchFeederToEtfTests(unittest.TestCase):
    """三例必须命中对应场内 ETF，不被同指数其他公司干扰。"""

    def test_yifangda_links_to_588080(self) -> None:
        hit = match_feeder_to_etf("易方达上证科创50联接C", _SAMPLE_ETFS)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.etf_code, "588080")
        self.assertEqual(hit.etf_name, "科创50ETF易方达")
        self.assertEqual(hit.matched_by, "name_company_index")
        self.assertEqual(hit.confidence, "high")

    def test_huaxia_links_to_588000(self) -> None:
        hit = match_feeder_to_etf("华夏科创50ETF联接A", _SAMPLE_ETFS)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.etf_code, "588000")

    def test_gongyin_links_to_588050(self) -> None:
        hit = match_feeder_to_etf("工银科创50ETF联接C", _SAMPLE_ETFS)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.etf_code, "588050")

    def test_plain_index_beats_same_company_style_variant(self) -> None:
        etfs = [
            {"code": "510330", "name": "沪深300ETF华夏"},
            {"code": "159510", "name": "沪深300价值ETF华夏"},
            {"code": "159523", "name": "沪深300成长ETF华夏"},
            {"code": "510300", "name": "沪深300ETF华泰柏瑞"},
        ]
        hit = match_feeder_to_etf("华夏沪深300ETF联接A", etfs)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.etf_code, "510330")
        self.assertEqual(hit.confidence, "high")

    def test_benchmark_cross_check_upgrades_index_only(self) -> None:
        etfs = [{"code": "510300", "name": "沪深300ETF华泰柏瑞"}]
        hit = match_feeder_to_etf(
            "某司沪深300ETF联接A",
            etfs,
            company_tokens=["某司"],
            benchmark="沪深300指数收益率×95%＋活期存款利率×5%",
        )
        self.assertIsNotNone(hit)
        self.assertEqual(hit.etf_code, "510300")
        self.assertEqual(hit.confidence, "high")
        self.assertEqual(hit.matched_by, "benchmark_cross")


class UpsertIdempotentTests(unittest.TestCase):
    """临时 sqlite：同一 fund_code 再写不新增行。"""

    def setUp(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._tmp_path = path
        self._old_path = os.environ.get("DB_PATH")
        os.environ["DB_PATH"] = path
        from app import db as database
        from app.db import reset_after_fork

        reset_after_fork()
        schema = Path(__file__).resolve().parents[1].joinpath("schema_sqlite.sql")
        database.init_db(schema.read_text(encoding="utf-8"))
        self.database = database

    def tearDown(self) -> None:
        from app.db import reset_after_fork

        reset_after_fork()
        if self._old_path is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = self._old_path
        try:
            os.unlink(self._tmp_path)
        except OSError:
            pass

    def test_upsert_same_fund_code_is_idempotent(self) -> None:
        from app.fund_etf_linkage import crud

        payload = {
            "fund_code": "011609",
            "fund_name": "易方达上证科创50联接C",
            "etf_code": "588080",
            "etf_name": "科创50ETF易方达",
            "matched_by": "name_company_index",
            "confidence": "high",
        }
        crud.upsert_linkage(payload)
        crud.upsert_linkage(
            {**payload, "confidence": "high", "etf_name": "科创50ETF易方达"}
        )
        rows = self.database.select("fund_etf_linkage")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["fund_code"], "011609")
        self.assertEqual(rows[0]["etf_code"], "588080")


if __name__ == "__main__":
    unittest.main()
