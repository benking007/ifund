"""MySQL backend contract tests; all driver I/O is mocked."""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, call, patch

import pymysql

from app.db.base import UniqueViolation
from app.db.mysql import MysqlDatabase


class MysqlDatabaseTests(unittest.TestCase):
    """Verify SQL translation and driver interaction without a MySQL server."""

    def setUp(self) -> None:
        self.connection = MagicMock(name="mysql_connection")
        self.cursor = MagicMock(name="mysql_cursor")
        cursor_context = self.connection.cursor.return_value
        cursor_context.__enter__.return_value = self.cursor
        cursor_context.__exit__.return_value = None
        self.cursor.fetchall.return_value = []
        self.cursor.fetchone.return_value = None
        self.connect = patch(
            "app.db.mysql.pymysql.connect", return_value=self.connection
        )
        self.connect.start()
        self.addCleanup(self.connect.stop)
        self.db = MysqlDatabase()

    def test_connection_is_lazy_and_uses_ifund_environment_defaults(self) -> None:
        with patch.dict(
            os.environ,
            {
                "IFUND_DB_HOST": "db.example",
                "IFUND_DB_PORT": "3307",
                "IFUND_DB_USER": "tester",
                "IFUND_DB_PASSWORD": "secret",
                "IFUND_DB_NAME": "fund_test",
            },
            clear=False,
        ):
            connect = patch(
                "app.db.mysql.pymysql.connect", return_value=self.connection
            )
            with connect as mocked_connect:
                db = MysqlDatabase()
                self.assertFalse(mocked_connect.called)
                db.select("funds")

        kwargs = mocked_connect.call_args.kwargs
        self.assertEqual(kwargs["host"], "db.example")
        self.assertEqual(kwargs["port"], 3307)
        self.assertEqual(kwargs["user"], "tester")
        self.assertEqual(kwargs["password"], "secret")
        self.assertEqual(kwargs["database"], "fund_test")
        self.assertFalse(kwargs["autocommit"])

    def test_db_entrypoint_dispatches_mysql_without_connecting(self) -> None:
        from app import db as database

        with (
            patch.dict(os.environ, {"DB_BACKEND": "mysql"}, clear=False),
            patch.object(database, "_STATE", {}),
        ):
            selected = database.get_db()

        self.assertIsInstance(selected, MysqlDatabase)

    def test_select_translates_filters_order_and_pagination(self) -> None:
        self.db.select(
            "funds",
            [
                ("status", "eq.active"),
                ("name", "ilike.*alpha*"),
                ("code", "in.(A,B)"),
                ("or", "(type.eq.stock,type.ilike.*index*)"),
                ("order", "code.desc"),
                ("limit", 10),
                ("offset", 20),
            ],
        )

        self.cursor.execute.assert_called_once_with(
            "SELECT * FROM `funds` WHERE `status` = %s "
            "AND LOWER(`name`) LIKE LOWER(%s) AND `code` IN (%s,%s) "
            "AND (`type` = %s OR LOWER(`type`) LIKE LOWER(%s)) "
            "ORDER BY `code` DESC LIMIT %s OFFSET %s",
            ["active", "%alpha%", "A", "B", "stock", "%index%", 10, 20],
        )

    def test_insert_fetches_new_row_and_batch_insert_uses_upsert(self) -> None:
        self.cursor.lastrowid = 7
        self.cursor.fetchone.return_value = {"id": 7, "code": "000001", "name": "One"}

        row = self.db.insert("funds", {"code": "000001", "name": "One"})

        self.assertEqual(row["id"], 7)
        self.assertEqual(
            self.cursor.execute.call_args_list,
            [
                call(
                    "INSERT INTO `funds` (`code`,`name`) VALUES (%s,%s)",
                    ["000001", "One"],
                ),
                call("SELECT * FROM `funds` WHERE `id` = %s", [7]),
            ],
        )
        self.connection.commit.assert_called_once()

        self.cursor.reset_mock()
        self.connection.commit.reset_mock()
        self.db.batch_insert(
            "funds",
            [{"code": "000001", "name": "One"}, {"code": "000002", "name": "Two"}],
            batch_size=1,
        )

        upsert_sql = (
            "INSERT INTO `funds` (`code`,`name`) VALUES (%s,%s) "
            "ON DUPLICATE KEY UPDATE `code` = VALUES(`code`), `name` = VALUES(`name`)"
        )
        self.assertEqual(
            self.cursor.executemany.call_args_list,
            [
                call(upsert_sql, [["000001", "One"]]),
                call(upsert_sql, [["000002", "Two"]]),
            ],
        )
        self.connection.commit.assert_called_once()

    def test_update_and_delete_use_parameterized_equal_filters(self) -> None:
        self.db.update(
            "funds", {"code": "000001", "type": "stock"}, {"name": "Updated"}
        )
        self.db.delete("funds", {"code": "000001"})
        self.db.delete("funds")

        self.assertEqual(
            self.cursor.execute.call_args_list,
            [
                call(
                    "UPDATE `funds` SET `name` = %s WHERE `code` = %s AND `type` = %s",
                    ["Updated", "000001", "stock"],
                ),
                call("DELETE FROM `funds` WHERE `code` = %s", ["000001"]),
                call("DELETE FROM `funds`", []),
            ],
        )
        self.assertEqual(self.connection.commit.call_count, 3)

    def test_duplicate_key_maps_to_unique_violation_and_rolls_back(self) -> None:
        self.cursor.execute.side_effect = pymysql.err.IntegrityError(
            1062, "Duplicate entry '000001' for key 'code'"
        )

        with self.assertRaises(UniqueViolation):
            self.db.insert("funds", {"code": "000001", "name": "One"})

        self.connection.rollback.assert_called_once()

    def test_init_db_converts_sqlite_schema_to_idempotent_mysql_statements(
        self,
    ) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS ix_users_username ON users (username);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_running ON fetch_tasks (task_type)
            WHERE status = 'running';
        """

        self.db.init_db(schema)

        statements = [item.args[0] for item in self.cursor.execute.call_args_list]
        table_ddl = statements[0]
        self.assertIn("BIGINT PRIMARY KEY AUTO_INCREMENT", table_ddl)
        self.assertIn("VARCHAR(255)", table_ddl)
        self.assertIn("`created_at` DATETIME DEFAULT CURRENT_TIMESTAMP", table_ddl)
        self.assertIn("DEFAULT CURRENT_TIMESTAMP", table_ddl)
        self.assertNotIn("AUTOINCREMENT", table_ddl)
        self.assertNotIn("datetime('now')", table_ddl)
        self.assertIn(
            "CREATE INDEX `ix_users_username` ON `users` (`username`)", statements[1]
        )
        self.assertIn("CREATE UNIQUE INDEX `ux_running`", statements[2])
        self.assertNotIn(" WHERE status = 'running'", statements[2])
        self.connection.commit.assert_called_once()

    def test_fund_details_join_uses_mysql_placeholders_and_identifiers(self) -> None:
        self.cursor.fetchone.return_value = {"n": 1}
        self.cursor.fetchall.return_value = [{"code": "000001"}]

        total, rows = self.db.list_funds_with_details(
            [("code", "eq.000001")],
            [("fund_manager", "ilike.*Alice*")],
            4,
            8,
            [("scale", "desc")],
        )

        self.assertEqual((total, rows), (1, [{"code": "000001"}]))
        count_sql, count_params = self.cursor.execute.call_args_list[0].args
        page_sql, page_params = self.cursor.execute.call_args_list[1].args
        self.assertIn("LEFT JOIN `fund_details`", count_sql)
        self.assertIn("`f`.`code` = %s", count_sql)
        self.assertEqual(count_params, ["000001", "%Alice%"])
        self.assertIn("ORDER BY `d`.`scale` DESC LIMIT %s OFFSET %s", page_sql)
        self.assertEqual(page_params, ["000001", "%Alice%", 8, 4])

    def test_industry_mapping_translates_sqlite_glob_and_concat(self) -> None:
        self.cursor.fetchall.return_value = [{"stock_code": "600000", "_total": 1}]

        total, rows = self.db.list_industry_mapping(
            market="A",
            label_kw="银行",
            status="covered",
            keyword="浦发",
            skip=2,
            limit=5,
        )

        self.assertEqual(total, 1)
        self.assertEqual(rows, [{"stock_code": "600000"}])
        sql, params = self.cursor.execute.call_args.args
        self.assertIn("REGEXP '^[0-9]{6}$'", sql)
        self.assertIn("CONCAT(", sql)
        self.assertIn("COUNT(*) OVER ()", sql)
        self.assertIn("LIMIT %s OFFSET %s", sql)
        self.assertEqual(params[-2:], [5, 2])

    def test_manager_summary_uses_mysql_json_search(self) -> None:
        self.cursor.fetchone.return_value = {"n": 2}
        self.cursor.fetchall.return_value = [{"code": "000001"}]

        total, rows = self.db.list_manager_summary(
            keyword="Alice",
            coverage="covered",
            preset_id=3,
            skip=1,
            limit=6,
            order_field="tenure_days",
            order_dir="desc",
        )

        self.assertEqual((total, rows), (2, [{"code": "000001"}]))
        count_sql, count_params = self.cursor.execute.call_args_list[0].args
        page_sql, page_params = self.cursor.execute.call_args_list[1].args
        self.assertIn("JSON_SEARCH", count_sql)
        self.assertIn("ORDER BY `t`.`tenure_days` DESC LIMIT %s OFFSET %s", page_sql)
        self.assertEqual(count_params, [3, "%Alice%", "%Alice%", "%Alice%", "%Alice%"])
        self.assertEqual(page_params[-2:], [6, 1])


if __name__ == "__main__":
    unittest.main()
