"""基金只读路由的日期、费用与分红契约。"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from flask import Flask

from app.fund.api.router import bp
from app.fund_nav.crud import div_split_crud, nav_crud


class FundReadRouteTests(unittest.TestCase):
    """验证路由参数下推和只读响应形状。"""

    @classmethod
    def setUpClass(cls) -> None:
        app = Flask(__name__)
        app.register_blueprint(bp)
        cls.client = app.test_client()

    def test_nav_normalises_and_forwards_date_window(self) -> None:
        """YYYYMMDD 日期须转换后下推，响应只包含窗口内数据。"""
        rows = [
            {"date": "2026-09-01", "nav": 1.1, "unit_nav": 1.0, "adj_nav": None, "adj_src": None},
            {"date": "2026-09-02", "nav": 1.2, "unit_nav": 1.1, "adj_nav": None, "adj_src": None},
            {"date": "2026-09-03", "nav": 1.3, "unit_nav": 1.2, "adj_nav": None, "adj_src": None},
        ]
        with patch.object(nav_crud, "recent_series_dated_with_adj", return_value=rows) as query:
            response = self.client.get(
                "/api/fund/000001/nav?start_date=20260901&end_date=20260903"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["date"] for item in response.get_json()["items"]], [
            "2026-09-01", "2026-09-02", "2026-09-03",
        ])
        query.assert_called_once_with(
            "000001", 750, start_date="2026-09-01", end_date="2026-09-03"
        )

    def test_nav_without_dates_keeps_limit_call(self) -> None:
        """无日期参数时继续使用原有最近 N 条调用。"""
        with patch.object(nav_crud, "recent_series_dated_with_adj", return_value=[]) as query:
            response = self.client.get("/api/fund/000001/nav?limit=12")
        self.assertEqual(response.status_code, 200)
        query.assert_called_once_with("000001", 12)

    def test_fee_returns_null_for_unavailable_sales_service_fee(self) -> None:
        """本地没有销售服务费字段时必须显式返回 null。"""
        row = {
            "fund_code": "000003",
            "ts_code": "000003.OF",
            "management_fee": 0.75,
            "custodian_fee": 0.2,
        }
        with patch("app.fund.api.router.database.select_one", return_value=row):
            response = self.client.get("/api/fund/fee?ts_code=000003.OF")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["management_fee"], 0.75)
        self.assertEqual(payload["custodian_fee"], 0.2)
        self.assertIsNone(payload["sales_service_fee"])

    def test_div_forwards_dates_and_marks_missing_split_coverage(self) -> None:
        """分红窗口下推，并如实声明当前没有结构化拆分事件。"""
        rows = [{
            "fund_code": "000001", "ts_code": "000001.OF",
            "ex_date": "2025-09-22", "event_type": "div", "cash_per_unit": 0.01,
        }]
        with patch("app.fund.api.router.div_split_crud.list_events", return_value=rows) as query:
            response = self.client.get(
                "/api/fund/div?ts_code=000001&start_date=20250101&end_date=20251231"
            )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["items"], rows)
        self.assertFalse(payload["split_coverage"]["structured"])
        query.assert_called_once_with(
            "000001", start_date="2025-01-01", end_date="2025-12-31"
        )


class NavCrudDateFilterTests(unittest.TestCase):
    """验证净值日期条件实际进入数据库查询。"""

    def test_date_filters_are_inclusive_and_limit_is_preserved(self) -> None:
        """净值 CRUD 同时携带日期索引条件与数量上限。"""
        with patch.object(nav_crud.database, "select", return_value=[]) as select:
            nav_crud.recent_series_dated_with_adj(
                "000001", 750, start_date="2026-09-01", end_date="2026-09-03"
            )
        params = select.call_args.args[1]
        self.assertIn(("trade_date", "gte.2026-09-01"), params)
        self.assertIn(("trade_date", "lte.2026-09-03"), params)
        self.assertIn(("limit", 750), params)


class DivCrudDateFilterTests(unittest.TestCase):
    """验证分红日期条件实际进入数据库查询。"""

    def test_date_filters_are_inclusive(self) -> None:
        """分红 CRUD 应将除权日起止日期下推。"""
        with patch.object(div_split_crud.database, "select", return_value=[]) as select:
            div_split_crud.list_events(
                "000001", start_date="2025-01-01", end_date="2025-12-31"
            )
        params = select.call_args.args[1]
        self.assertIn(("ex_date", "gte.2025-01-01"), params)
        self.assertIn(("ex_date", "lte.2025-12-31"), params)


if __name__ == "__main__":
    unittest.main()
