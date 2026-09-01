"""fund_detail 过期判定的回归测试（含 source_unavailable 占位记录）。"""
from __future__ import annotations

import datetime
from unittest import TestCase
from unittest.mock import patch

from app.fund_detail.crud import detail_crud


def _row(**overrides) -> dict:
    row = {
        "fund_code": "000002",
        "detail_json": "{}",
        "fetch_time": datetime.datetime.now().isoformat(timespec="seconds"),
        "trade_date": "2026-08-31",
        "scale": 12.3,
    }
    row.update(overrides)
    return row


class DetailExpiryTests(TestCase):
    def test_no_record_expired(self) -> None:
        with patch.object(detail_crud, "get_detail", return_value=None):
            self.assertTrue(detail_crud.is_expired("000002", "2026-08-31"))

    def test_fresh_record_not_expired(self) -> None:
        with patch.object(detail_crud, "get_detail", return_value=_row()):
            self.assertFalse(detail_crud.is_expired("000002", "2026-08-31"))

    def test_trade_date_mismatch_expired(self) -> None:
        with patch.object(
            detail_crud, "get_detail", return_value=_row(trade_date="2026-07-31")
        ):
            self.assertTrue(detail_crud.is_expired("000002", "2026-08-31"))

    def test_scale_missing_expired(self) -> None:
        with patch.object(detail_crud, "get_detail", return_value=_row(scale=None)):
            self.assertTrue(detail_crud.is_expired("000002", "2026-08-31"))

    def test_source_unavailable_fresh_skipped(self) -> None:
        """占位记录 7 天内视为无需刷新（即使 trade_date 落后 / scale 缺失）。"""
        row = _row(
            detail_json='{"source_unavailable": true}',
            trade_date="2026-08-31",
            scale=None,
        )
        with patch.object(detail_crud, "get_detail", return_value=row):
            self.assertFalse(detail_crud.is_expired("000002", "2026-08-31"))

    def test_source_unavailable_stale_reprobes(self) -> None:
        """占位记录超过 7 天 → 过期自动重探。"""
        old = (datetime.datetime.now() - datetime.timedelta(days=8)).isoformat(
            timespec="seconds"
        )
        row = _row(
            detail_json='{"source_unavailable": true}',
            fetch_time=old,
            trade_date="2026-08-31",
            scale=None,
        )
        with patch.object(detail_crud, "get_detail", return_value=row):
            self.assertTrue(detail_crud.is_expired("000002", "2026-08-31"))

    def test_source_unavailable_malformed_json_not_treated(self) -> None:
        """detail_json 损坏 → 按普通记录判据。"""
        row = _row(detail_json="not-json{{{", scale=None)
        with patch.object(detail_crud, "get_detail", return_value=row):
            self.assertTrue(detail_crud.is_expired("000002", "2026-08-31"))
