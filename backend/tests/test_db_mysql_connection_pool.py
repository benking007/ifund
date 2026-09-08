"""MySQL bounded connection pool tests; no real driver I/O is performed."""

from __future__ import annotations

import os
import threading
import unittest
from unittest.mock import MagicMock, call, patch

import pymysql

from app.db.mysql import MysqlConnectionPoolTimeout, MysqlDatabase


def _mock_connection(name: str) -> tuple[MagicMock, MagicMock]:
    connection = MagicMock(name=name)
    cursor = MagicMock(name=f"{name}_cursor")
    cursor_context = connection.cursor.return_value
    cursor_context.__enter__.return_value = cursor
    cursor_context.__exit__.return_value = None
    cursor.fetchall.return_value = []
    cursor.fetchone.return_value = None
    return connection, cursor


class MysqlConnectionPoolTests(unittest.TestCase):
    """Verify bounded checkout/checkin and transaction ownership semantics."""

    def test_db_methods_checkout_and_return_the_same_pooled_connection(self) -> None:
        """Ordinary calls borrow briefly, clean state, and reuse one connection."""
        connection, _ = _mock_connection("connection")
        with patch("app.db.mysql.pymysql.connect", return_value=connection) as connect:
            database = MysqlDatabase()
            self.addCleanup(database.close)

            database.select("funds")
            database.select("funds")

        connect.assert_called_once()
        connection.ping.assert_called_once_with(reconnect=False)
        self.assertEqual(connection.rollback.call_count, 2)
        self.assertEqual(len(database._pool._idle), 1)  # pylint: disable=protected-access

    def test_concurrent_checkout_never_exceeds_limit_and_times_out(self) -> None:
        """Concurrent creation respects the configured cap and wait deadline."""
        first, _ = _mock_connection("first")
        second, _ = _mock_connection("second")
        acquired = threading.Barrier(3)
        release = threading.Event()
        failures: list[BaseException] = []

        with (
            patch.dict(
                os.environ,
                {"IFUND_DB_POOL_SIZE": "2", "IFUND_DB_POOL_TIMEOUT": "0.02"},
                clear=False,
            ),
            patch(
                "app.db.mysql.pymysql.connect", side_effect=[first, second]
            ) as connect,
        ):
            database = MysqlDatabase()
            self.addCleanup(database.close)

            def hold_connection() -> None:
                try:
                    connection = database._pool.checkout()  # pylint: disable=protected-access
                    acquired.wait(timeout=1)
                    release.wait(timeout=1)
                    database._pool.checkin(connection)  # pylint: disable=protected-access
                except BaseException as exc:  # pylint: disable=broad-exception-caught
                    failures.append(exc)

            threads = [threading.Thread(target=hold_connection) for _ in range(2)]
            for thread in threads:
                thread.start()
            acquired.wait(timeout=1)

            with self.assertRaisesRegex(
                MysqlConnectionPoolTimeout, "上限 2.*超时 0.02 秒"
            ):
                database._pool.checkout()  # pylint: disable=protected-access

            release.set()
            for thread in threads:
                thread.join(timeout=1)

        self.assertFalse(failures)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(connect.call_count, 2)
        first.rollback.assert_called_once()
        second.rollback.assert_called_once()

    def test_method_exception_still_rolls_back_and_returns_connection(self) -> None:
        """Exceptional method exits cleanly and returns the borrowed connection."""
        connection, cursor = _mock_connection("connection")
        cursor.execute.side_effect = RuntimeError("query failed")
        with patch("app.db.mysql.pymysql.connect", return_value=connection) as connect:
            database = MysqlDatabase()
            self.addCleanup(database.close)

            with self.assertRaisesRegex(RuntimeError, "query failed"):
                database.select("funds")
            cursor.execute.side_effect = None
            database.select("funds")

        connect.assert_called_once()
        connection.rollback.assert_has_calls([call(), call()])
        connection.ping.assert_called_once_with(reconnect=False)

    def test_rollback_failure_discards_connection_and_rebuilds(self) -> None:
        """A connection that cannot be cleaned is closed and replaced lazily."""
        broken, _ = _mock_connection("broken")
        replacement, _ = _mock_connection("replacement")
        broken.rollback.side_effect = pymysql.err.OperationalError(
            2006, "MySQL server has gone away"
        )
        with patch(
            "app.db.mysql.pymysql.connect", side_effect=[broken, replacement]
        ) as connect:
            database = MysqlDatabase()
            self.addCleanup(database.close)

            database.select("funds")
            database.select("funds")

        self.assertEqual(connect.call_count, 2)
        broken.close.assert_called_once()
        replacement.rollback.assert_called_once()

    def test_failed_ping_discards_connection_and_rebuilds(self) -> None:
        """An idle connection that fails validation is closed and replaced."""
        broken, _ = _mock_connection("broken")
        replacement, _ = _mock_connection("replacement")
        broken.ping.side_effect = pymysql.err.OperationalError(
            2013, "Lost connection to MySQL server"
        )
        with patch(
            "app.db.mysql.pymysql.connect", side_effect=[broken, replacement]
        ) as connect:
            database = MysqlDatabase()
            self.addCleanup(database.close)

            database.select("funds")
            database.select("funds")

        self.assertEqual(connect.call_count, 2)
        broken.ping.assert_called_once_with(reconnect=False)
        broken.close.assert_called_once()
        replacement.rollback.assert_called_once()

    def test_close_releases_idle_and_checked_out_connections(self) -> None:
        """Pool shutdown closes every tracked connection and rejects new work."""
        idle, _ = _mock_connection("idle")
        checked_out, _ = _mock_connection("checked_out")
        with patch(
            "app.db.mysql.pymysql.connect", side_effect=[idle, checked_out]
        ):
            database = MysqlDatabase()
            idle_connection = database._pool.checkout()  # pylint: disable=protected-access
            database._pool.checkin(idle_connection)  # pylint: disable=protected-access
            checked_out_connection = database._pool.checkout()  # pylint: disable=protected-access
            other_connection = database._pool.checkout()  # pylint: disable=protected-access

            database.close()

        self.assertIs(checked_out_connection, idle)
        self.assertIs(other_connection, checked_out)
        idle.close.assert_called_once()
        checked_out.close.assert_called_once()
        with self.assertRaisesRegex(RuntimeError, "连接池已关闭"):
            database._pool.checkout()  # pylint: disable=protected-access

    def test_nested_transaction_uses_savepoints_and_returns_only_at_outer_exit(
        self,
    ) -> None:
        """Nested scopes use savepoints while only the outer scope checks in."""
        connection, cursor = _mock_connection("connection")
        with patch("app.db.mysql.pymysql.connect", return_value=connection) as connect:
            database = MysqlDatabase()
            self.addCleanup(database.close)

            with database.transaction() as outer:
                self.assertIs(outer, connection)
                with database.transaction() as inner:
                    self.assertIs(inner, connection)
                    database.select("funds")
                try:
                    with database.transaction():
                        raise ValueError("nested failure")
                except ValueError:
                    pass
                self.assertEqual(
                    len(database._pool._idle), 0  # pylint: disable=protected-access
                )
                connection.rollback.assert_not_called()

        connect.assert_called_once()
        connection.begin.assert_called_once()
        connection.commit.assert_called_once()
        connection.rollback.assert_called_once()
        self.assertEqual(
            cursor.execute.call_args_list,
            [
                call("SAVEPOINT ifund_nested_1"),
                call("SELECT * FROM `funds`", []),
                call("RELEASE SAVEPOINT ifund_nested_1"),
                call("SAVEPOINT ifund_nested_1"),
                call("ROLLBACK TO SAVEPOINT ifund_nested_1"),
                call("RELEASE SAVEPOINT ifund_nested_1"),
            ],
        )
        self.assertEqual(len(database._pool._idle), 1)  # pylint: disable=protected-access
        self.assertEqual(database._local.transaction_depth, 0)  # pylint: disable=protected-access
        self.assertIsNone(
            database._local.transaction_connection  # pylint: disable=protected-access
        )


if __name__ == "__main__":
    unittest.main()
