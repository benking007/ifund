"""Tushare 客户端共享总闸、token 优先级与重试测试。"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

from app.fund_nav.fetch import tushare_client


class TushareTokenTests(TestCase):
    """fin-data 服务 token 不应被继承的失效 token 覆盖。"""

    def test_fin_data_token_wins_over_environment(self) -> None:
        with (
            patch.dict("os.environ", {"TUSHARE_TOKEN": "stale-token"}),
            patch.object(tushare_client, "_token_from_file", return_value="service-token"),
        ):
            self.assertEqual(tushare_client.get_token(), "service-token")


class SharedGlobalLimiterTests(TestCase):
    """iFund 请求槽写入 fin-data 兼容状态文件。"""

    def test_two_reservations_are_spaced_at_client_rate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "tushare-rate.json"
            with (
                patch.dict(
                    "os.environ",
                    {
                        "FIN_DATA_TUSHARE_LIMITER_STATE": str(state_path),
                        "IFUND_TUSHARE_RATE_PER_MIN": "20",
                    },
                ),
                patch.object(tushare_client.time, "time", return_value=100.0),
                patch.object(tushare_client.time, "sleep") as sleep,
            ):
                limiter = tushare_client._SharedGlobalLimiter()
                self.assertEqual(limiter.wait(), 0.0)
                self.assertEqual(limiter.wait(), 3.0)

            sleep.assert_called_once_with(3.0)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["next_at"], 106.0)
            self.assertEqual(state["baseline_rate_per_min"], 100.0)


class TushareRetryTests(TestCase):
    """瞬时频限在共享总闸降速后重试，错误语义保持统一。"""

    def test_rate_limit_downshifts_and_retries(self) -> None:
        limited = MagicMock()
        limited.json.return_value = {"code": -2001, "msg": "每分钟频率超限"}
        success = MagicMock()
        success.json.return_value = {
            "code": 0,
            "data": {"fields": ["ts_code"], "items": [["000001.OF"]]},
        }
        limiter = MagicMock()
        with (
            patch.object(tushare_client, "get_token", return_value="test-token"),
            patch.object(tushare_client, "_get_limiter", return_value=limiter),
            patch.object(
                tushare_client.requests,
                "post",
                side_effect=[limited, success],
            ) as post,
        ):
            rows = tushare_client.call("fund_basic")

        self.assertEqual(rows, [{"ts_code": "000001.OF"}])
        self.assertEqual(post.call_count, 2)
        self.assertEqual(limiter.wait.call_count, 2)
        limiter.on_rate_limit.assert_called_once()
