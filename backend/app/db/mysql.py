"""MySQL implementation of the small, backend-neutral database contract.

The application deliberately keeps its PostgREST-like filter vocabulary in the
database layer.  This module translates that vocabulary to MySQL placeholders
and identifiers, while retaining the transaction boundaries used by the
SQLite implementation.

``init_db`` accepts the existing SQLite schema rather than requiring a second
schema file.  The converter is intentionally small and deterministic: it
rewrites SQLite's auto-increment/default syntax, gives ``TEXT`` columns a
MySQL-safe type, removes unsupported ``IF NOT EXISTS`` from index DDL, and
maps SQLite partial indexes to MySQL indexes.  Unique partial indexes use a
functional ``IF(..., 1, NULL)`` key part, so the converted schema targets
MySQL 8.0.13+ (the version family that supports functional indexes).
"""

# The schema converter and backend intentionally live together so init_db can
# reuse the same private SQL helpers without widening the module API.
# pylint: disable=too-many-lines,duplicate-code

from __future__ import annotations

import atexit
import os
import re
import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager

import pymysql

from .base import Database, UniqueViolation

# Keep this list in sync with schema_sqlite.sql.  It protects every operation
# whose table name is interpolated into SQL; values remain parameterized.
VALID_TABLES: frozenset[str] = frozenset(
    {
        "users",
        "api_tokens",
        "funds",
        "fund_types",
        "query_presets",
        "fund_snapshots",
        "fund_details",
        "fetch_tasks",
        "trade_dates",
        "fund_holdings",
        "fund_nav",
        "fund_div_split",
        "event_scan_status",
        "nav_repair_queue",
        "fund_cum_return",
        "fund_manager_tenure",
        "stock_industry",
        "portfolios",
        "user_holdings",
        "holding_txns",
        "fund_ai_analysis",
        "app_settings",
        "perpetual_portfolio",
        "fund_etf_linkage",
        "fund_sync_state",
        "fund_ts_code_map",
        "fund_ts_code_quarantine",
        "fund_ts_code_alias",
        "fund_company",
        "fund_basic_ext",
        "fund_share",
        "fund_share_sync_state",
    }
)

SORTABLE_DETAIL = {
    "scale",
    "return_ytd",
    "drawdown_ytd",
    "sharpe_3y",
    "sharpe_1y",
    "max_drawdown_3y",
    "max_drawdown_1y",
    "position_stock",
}
SORTABLE_AI = {"skill_score", "rating", "tenure_years"}

_RESULT_COLS = [
    "f.`id` AS id",
    "f.`code` AS code",
    "f.`name` AS name",
    "f.`type` AS type",
    "f.`fund_type` AS fund_type",
    "d.`fund_manager` AS fund_manager",
    "d.`scale` AS scale",
    "d.`sharpe_3y` AS sharpe_3y",
    "d.`sharpe_1y` AS sharpe_1y",
    "d.`max_drawdown_3y` AS max_drawdown_3y",
    "d.`max_drawdown_1y` AS max_drawdown_1y",
    "d.`position_stock` AS position_stock",
    "d.`position_bond` AS position_bond",
    "d.`return_ytd` AS return_ytd",
    "d.`drawdown_ytd` AS drawdown_ytd",
]

_MGR_SORTABLE = {
    "code": "f",
    "name": "f",
    "fund_type": "d",
    "fund_company": "d",
    "scale": "d",
    "fund_manager": "d",
    "return_1y": "d",
    "return_3y": "d",
    "managers": "t",
    "tenure_days": "t",
    "tenure_return": "t",
    "start_date": "t",
}

_OR_SPLIT_RE = re.compile(r",(?=[^()]*(?:\(|$))")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_LONG_TEXT_COLUMNS = frozenset(
    {
        "detail_json",
        "filters_json",
        "items_json",
        "raw_data",
        "result_json",
        "tags",
        "skill_reason",
        "concentration_reason",
        "hard_thesis",
        "turnover_note",
        "data_basis",
        "invest_strategy",
        "invest_target",
        "benchmark",
        "last_error",
        "source_evidence",
    }
)


def _check_table(table: str) -> None:
    if table not in VALID_TABLES:
        raise ValueError(f"非法表名: {table}")


def _quote_identifier(identifier: str) -> str:
    """Quote one identifier or a dotted identifier without accepting SQL."""
    parts = str(identifier).split(".")
    if not parts or any(
        not _IDENTIFIER_RE.fullmatch(part.strip("`")) for part in parts
    ):
        raise ValueError(f"非法标识符: {identifier}")
    return ".".join(f"`{part.strip('`').replace('`', '``')}`" for part in parts)


def _quote_col(col: str) -> str:
    return _quote_identifier(col)


def _prefixed(col: str, prefix: str) -> str:
    if not prefix or "." in str(col):
        return str(col)
    return f"{prefix}.{col}"


def _parse_in_list(raw: str) -> list[str]:
    raw = str(raw).strip()
    if raw.startswith("(") and raw.endswith(")"):
        raw = raw[1:-1]
    return [item.strip() for item in raw.split(",") if item.strip()]


def _in_clause(col: str, raw: str, negate: bool) -> tuple[str, list[str]]:
    items = _parse_in_list(raw)
    placeholders = ",".join(["%s"] * len(items)) or "NULL"
    operator = "NOT IN" if negate else "IN"
    return f"{col} {operator} ({placeholders})", items


_OPERATORS = [
    (
        "not.ilike.",
        lambda c, v: (f"LOWER({c}) NOT LIKE LOWER(%s)", [v.replace("*", "%")]),
    ),
    ("ilike.", lambda c, v: (f"LOWER({c}) LIKE LOWER(%s)", [v.replace("*", "%")])),
    ("not.in.", lambda c, v: _in_clause(c, v, True)),
    ("in.", lambda c, v: _in_clause(c, v, False)),
    ("neq.", lambda c, v: (f"{c} != %s", [v])),
    ("eq.", lambda c, v: (f"{c} = %s", [v])),
    ("gte.", lambda c, v: (f"{c} >= %s", [v])),
    ("gt.", lambda c, v: (f"{c} > %s", [v])),
    ("lte.", lambda c, v: (f"{c} <= %s", [v])),
    ("lt.", lambda c, v: (f"{c} < %s", [v])),
]


def _parse_filter(col: str, value) -> tuple[str, list]:
    quoted_col = _quote_col(col)
    text = str(value)
    for prefix, builder in _OPERATORS:
        if text.startswith(prefix):
            return builder(quoted_col, text[len(prefix) :])
    return f"{quoted_col} = %s", [text]


def _parse_or(value, prefix: str = "") -> tuple[str, list]:
    inner = str(value).strip()
    if inner.startswith("(") and inner.endswith(")"):
        inner = inner[1:-1]
    parts: list[str] = []
    params: list = []
    for clause in _OR_SPLIT_RE.split(inner):
        clause = clause.strip()
        if not clause:
            continue
        col, _, rest = clause.partition(".")
        sql, clause_params = _parse_filter(_prefixed(col, prefix), rest)
        parts.append(sql)
        params.extend(clause_params)
    return "(" + " OR ".join(parts) + ")", params


def _build_order(value, prefix: str = "") -> str:
    segments: list[str] = []
    for segment in str(value).split(","):
        segment = segment.strip()
        if not segment:
            continue
        if segment.endswith(".desc"):
            column, direction = segment[:-5], "DESC"
        elif segment.endswith(".asc"):
            column, direction = segment[:-4], "ASC"
        else:
            column, direction = segment, "ASC"
        segments.append(f"{_quote_col(_prefixed(column, prefix))} {direction}")
    return "ORDER BY " + ", ".join(segments) if segments else ""


def _quote_select(value) -> str:
    """Quote the simple column-list form used by the existing callers."""
    text = str(value).strip()
    if text == "*":
        return "*"
    distinct = ""
    if text.lower().startswith("distinct "):
        distinct, text = "DISTINCT ", text[9:].strip()
    columns = []
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        alias_match = re.fullmatch(
            r"([A-Za-z_][A-Za-z0-9_$]*)(?:\s+AS\s+([A-Za-z_][A-Za-z0-9_$]*))?",
            item,
            flags=re.IGNORECASE,
        )
        if not alias_match:
            raise ValueError(f"不支持的 select 表达式: {item}")
        column, alias = alias_match.groups()
        rendered = _quote_col(column)
        if alias:
            rendered += f" AS {_quote_col(alias)}"
        columns.append(rendered)
    if not columns:
        raise ValueError("select 不能为空")
    return distinct + ",".join(columns)


def _normalize_params(params):
    if params is None:
        return []
    if isinstance(params, dict):
        return list(params.items())
    return list(params)


def _build_clauses(params, prefix: str = "") -> dict:
    where_parts: list[str] = []
    where_params: list = []
    order = ""
    select = "*"
    limit = offset = None
    for key, value in _normalize_params(params):
        if key == "select":
            select = _quote_select(value)
        elif key == "order":
            order = _build_order(value, prefix)
        elif key == "limit":
            limit = int(value)
        elif key == "offset":
            offset = int(value)
        elif key == "or":
            sql, clause_params = _parse_or(value, prefix)
            where_parts.append(sql)
            where_params.extend(clause_params)
        else:
            sql, clause_params = _parse_filter(_prefixed(key, prefix), value)
            where_parts.append(sql)
            where_params.extend(clause_params)
    return {
        "where": " AND ".join(where_parts),
        "where_params": where_params,
        "order": order,
        "limit": limit,
        "offset": offset,
        "select": select,
    }


def _row_value(row, key: str, default=0):
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    return row[0]


def _error_code(exc: BaseException):
    args = getattr(exc, "args", ())
    return args[0] if args and isinstance(args[0], int) else getattr(exc, "errno", None)


def _is_duplicate_key(exc: BaseException) -> bool:
    return _error_code(exc) == 1062


def _is_duplicate_index(exc: BaseException) -> bool:
    # 1061 is duplicate key name; 1831 is emitted by some MySQL versions for
    # an already-existing index definition.
    return _error_code(exc) in {1061, 1831}


def _split_csv(value: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote = None
    for index, char in enumerate(value):
        if quote:
            if char == quote and (index == 0 or value[index - 1] != "\\"):
                quote = None
            continue
        if char in "'\"`":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(value[start:index].strip())
            start = index + 1
    parts.append(value[start:].strip())
    return [part for part in parts if part]


def _split_sql_statements(sql: str) -> list[str]:
    """Split DDL at semicolons outside quoted strings."""
    statements: list[str] = []
    start = 0
    quote = None
    for index, char in enumerate(sql):
        if quote:
            if char == quote and (index == 0 or sql[index - 1] != "\\"):
                quote = None
            continue
        if char in "'\"`":
            quote = char
        elif char == ";":
            statement = sql[start:index].strip()
            if statement:
                statements.append(statement)
            start = index + 1
    tail = sql[start:].strip()
    if tail:
        statements.append(tail)
    return statements


def _strip_sql_comments(sql: str) -> str:
    """Remove SQLite line comments while preserving quoted string contents."""
    output: list[str] = []
    quote = None
    index = 0
    while index < len(sql):
        char = sql[index]
        if quote:
            output.append(char)
            if char == quote and (index == 0 or sql[index - 1] != "\\"):
                quote = None
            index += 1
            continue
        if char in "'\"`":
            quote = char
            output.append(char)
            index += 1
        elif sql[index : index + 2] == "--":
            newline = sql.find("\n", index)
            if newline < 0:
                break
            output.append("\n")
            index = newline + 1
        else:
            output.append(char)
            index += 1
    return "".join(output)


def _indexed_columns(statements: list[str]) -> set[str]:
    columns: set[str] = set()
    pattern = re.compile(
        r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r"[A-Za-z_][A-Za-z0-9_$]*\s+ON\s+[`A-Za-z_][A-Za-z0-9_$`]*\s*\(([^)]*)\)",
        flags=re.IGNORECASE | re.DOTALL,
    )
    for statement in statements:
        match = pattern.search(statement)
        if not match:
            continue
        for part in _split_csv(match.group(1)):
            column_match = re.match(r"[`]?([A-Za-z_][A-Za-z0-9_$]*)[`]?", part.strip())
            if column_match:
                columns.add(column_match.group(1).lower())
    return columns


def _convert_text_columns(statement: str, indexed_columns: set[str]) -> str:
    opening = statement.find("(")
    closing = statement.rfind(")")
    if opening < 0 or closing <= opening:
        return statement

    # 表级 UNIQUE 约束内的列：SQLite 无索引长度限制，MySQL 组合唯一键
    # 超 3072 字节会失败（utf8mb4 × VARCHAR(255)）。短键列强制 VARCHAR(64)，
    # 64×4=256 字节，任意组合远低于上限。
    unique_cols: set[str] = set()
    for unique_match in re.finditer(
        r"\bUNIQUE\s*\(([^)]*)\)", statement, flags=re.IGNORECASE | re.DOTALL
    ):
        for part in _split_csv(unique_match.group(1)):
            col_match = re.match(r"[`]?([A-Za-z_][A-Za-z0-9_$]*)[`]?", part.strip())
            if col_match:
                unique_cols.add(col_match.group(1).lower())

    def replace(part: str) -> str:
        match = re.match(
            r"(?P<indent>\s*)(?P<name>`?[A-Za-z_][A-Za-z0-9_$]*`?)"
            r"(?P<gap>\s+)TEXT\b(?P<tail>.*)",
            part,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return part.strip()
        column = match.group("name").strip("`")
        tail = match.group("tail")
        upper_tail = tail.upper()
        large = (
            column.lower() in _LONG_TEXT_COLUMNS
            and column.lower() not in indexed_columns
            and "UNIQUE" not in upper_tail
            and "PRIMARY KEY" not in upper_tail
        )
        if "DEFAULT CURRENT_TIMESTAMP" in upper_tail:
            # CURRENT_TIMESTAMP is valid as a default for temporal columns,
            # not for MySQL VARCHAR/TEXT columns. SQLite stores these values as
            # TEXT, so promote only this explicitly timestamped subset.
            target_type = "DATETIME"
        elif large:
            # MySQL TEXT/LONGTEXT cannot carry the SQLite-style literal
            # default. These columns are optional payloads and callers already
            # provide their application defaults when reading/writing them.
            tail = re.sub(
                r"\s+DEFAULT\s+(?:\([^\n()]*\)|'[^\n']*'|\"[^\n\"]*\")",
                "",
                tail,
                flags=re.IGNORECASE,
            )
            target_type = "LONGTEXT"
        elif column.lower() in unique_cols:
            # 表级 UNIQUE 约束内的短键列：收紧长度避免组合唯一键超 3072 字节
            target_type = "VARCHAR(64)"
        else:
            target_type = "VARCHAR(255)"
        quoted = (
            match.group("name")
            if match.group("name").startswith("`")
            else f"`{match.group('name')}`"
        )
        return f"{match.group('indent')}{quoted}{match.group('gap')}{target_type}{tail}"

    body = statement[opening + 1 : closing]
    converted_body = ",\n    ".join(replace(part) for part in _split_csv(body))
    return (
        statement[: opening + 1]
        + "\n    "
        + converted_body
        + "\n"
        + statement[closing:]
    )


def _quote_index_part(part: str) -> str:
    match = re.fullmatch(
        r"[`]?([A-Za-z_][A-Za-z0-9_$]*)[`]?\s*(ASC|DESC)?",
        part.strip(),
        flags=re.IGNORECASE,
    )
    if not match:
        return part.strip()
    direction = f" {match.group(2).upper()}" if match.group(2) else ""
    return f"`{match.group(1)}`{direction}"


def _convert_index_statement(statement: str) -> str | None:
    pattern = re.compile(
        r"^CREATE\s+(?P<unique>UNIQUE\s+)?INDEX\s+"
        r"(?:IF\s+NOT\s+EXISTS\s+)?(?P<index>`?[A-Za-z_][A-Za-z0-9_$]*`?)\s+"
        r"ON\s+(?P<table>`?[A-Za-z_][A-Za-z0-9_$]*`?)\s*"
        r"\((?P<columns>[^)]*)\)\s*(?:WHERE\s+(?P<where>.*))?$",
        flags=re.IGNORECASE | re.DOTALL,
    )
    match = pattern.match(statement.strip())
    if not match:
        return None
    unique = bool(match.group("unique"))
    index_name = match.group("index").strip("`")
    table = match.group("table").strip("`")
    parts = [_quote_index_part(part) for part in _split_csv(match.group("columns"))]
    where = (match.group("where") or "").strip()
    if where and unique:
        # MySQL has no WHERE clause on indexes. A nullable functional key part
        # preserves the useful unique-when-predicate-is-true behavior because
        # InnoDB permits multiple NULLs in a unique index.
        parts.append(f"((IF({where}, 1, NULL)))")
    return (
        f"CREATE {'UNIQUE ' if unique else ''}INDEX `{index_name}` ON `{table}` "
        f"({', '.join(parts)})"
    )


def _convert_table_statement(statement: str, indexed_columns: set[str]) -> str:
    converted = re.sub(
        r"\bDEFAULT\s*\(\s*datetime\s*\(\s*['\"]now['\"]\s*\)\s*\)",
        "DEFAULT CURRENT_TIMESTAMP",
        statement,
        flags=re.IGNORECASE,
    )
    converted = re.sub(
        r"\bAUTOINCREMENT\b", "AUTO_INCREMENT", converted, flags=re.IGNORECASE
    )
    converted = re.sub(r"\bINTEGER\b", "BIGINT", converted, flags=re.IGNORECASE)
    converted = _convert_text_columns(converted, indexed_columns)
    converted = re.sub(
        r"^(\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?)([`A-Za-z_][A-Za-z0-9_$`]*)",
        lambda match: f"{match.group(1)}{_quote_identifier(match.group(2).strip('`'))}",
        converted,
        flags=re.IGNORECASE,
    )
    return converted


def _convert_schema_sql(schema_sql: str) -> list[str]:
    """Return executable MySQL statements converted from SQLite DDL."""
    statements = _split_sql_statements(_strip_sql_comments(schema_sql))
    indexed_columns = _indexed_columns(statements)
    converted: list[str] = []
    for statement in statements:
        index_statement = _convert_index_statement(statement)
        if index_statement is not None:
            converted.append(index_statement)
        elif re.match(r"^CREATE\s+TABLE\b", statement.strip(), flags=re.IGNORECASE):
            converted.append(_convert_table_statement(statement, indexed_columns))
        else:
            converted.append(statement.strip())
    return converted


class MysqlConnectionPoolTimeout(TimeoutError):
    """Raised when no MySQL connection becomes available before the deadline."""


class _MysqlConnectionPool:
    """Lazy, process-local bounded pool for PyMySQL connections."""

    def __init__(self, connect_kwargs: dict, max_size: int, checkout_timeout: float):
        if max_size <= 0:
            raise ValueError("IFUND_DB_POOL_SIZE 必须大于 0")
        if checkout_timeout <= 0:
            raise ValueError("IFUND_DB_POOL_TIMEOUT 必须大于 0")
        self._connect_kwargs = connect_kwargs
        self.max_size = max_size
        self.checkout_timeout = checkout_timeout
        self._condition = threading.Condition()
        self._idle = deque()
        self._connections: set = set()
        self._creating = 0
        self._closed = False

    @staticmethod
    def _close_connection(connection) -> None:
        try:
            connection.close()
        except pymysql.MySQLError:
            pass

    def _discard(self, connection) -> None:
        with self._condition:
            self._connections.discard(connection)
            self._condition.notify()
        self._close_connection(connection)

    def checkout(self):
        """Borrow a live connection, waiting at most ``checkout_timeout``."""
        deadline = time.monotonic() + self.checkout_timeout
        while True:
            create_connection = False
            with self._condition:
                if self._closed:
                    raise RuntimeError("MySQL 连接池已关闭")
                if self._idle:
                    connection = self._idle.pop()
                elif len(self._connections) + self._creating < self.max_size:
                    self._creating += 1
                    create_connection = True
                    connection = None
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise MysqlConnectionPoolTimeout(
                            "等待 MySQL 连接超时"
                            f"（上限 {self.max_size}，超时 {self.checkout_timeout:g} 秒）"
                        )
                    self._condition.wait(remaining)
                    continue

            if create_connection:
                try:
                    connection = pymysql.connect(**self._connect_kwargs)
                except BaseException:
                    with self._condition:
                        self._creating -= 1
                        self._condition.notify()
                    raise
                with self._condition:
                    self._creating -= 1
                    if self._closed:
                        close_connection = True
                    else:
                        self._connections.add(connection)
                        close_connection = False
                    self._condition.notify()
                if close_connection:
                    self._close_connection(connection)
                    raise RuntimeError("MySQL 连接池已关闭")
                return connection

            try:
                connection.ping(reconnect=False)
            except pymysql.MySQLError:
                self._discard(connection)
                continue
            return connection

    def checkin(self, connection, *, discard: bool = False) -> None:
        """Rollback all pending state and return a usable connection to the pool."""
        try:
            connection.rollback()
        except Exception:  # pylint: disable=broad-exception-caught
            self._discard(connection)
            return

        if discard:
            self._discard(connection)
            return

        with self._condition:
            if self._closed or connection not in self._connections:
                close_connection = True
            else:
                self._idle.append(connection)
                close_connection = False
                self._condition.notify()
        if close_connection:
            self._close_connection(connection)

    def close(self) -> None:
        """Prevent future checkouts and close every connection created by the pool."""
        with self._condition:
            if self._closed:
                return
            self._closed = True
            connections = list(self._connections)
            self._connections.clear()
            self._idle.clear()
            self._condition.notify_all()
        for connection in connections:
            self._close_connection(connection)


class MysqlDatabase(Database):
    """Lazily connected PyMySQL backend backed by a bounded process pool.

    Each database-layer method borrows and returns a connection. Only an active
    public ``transaction()`` keeps its connection for the transaction lifetime;
    nested transactions retain the existing savepoint semantics.
    """

    def __init__(self) -> None:
        self._connect_kwargs = {
            "host": os.getenv("IFUND_DB_HOST", "127.0.0.1"),
            "port": int(os.getenv("IFUND_DB_PORT", "3306")),
            "user": os.getenv("IFUND_DB_USER", "ifund"),
            "password": os.getenv("IFUND_DB_PASSWORD", ""),
            "database": os.getenv("IFUND_DB_NAME", "ifund"),
            "charset": "utf8mb4",
            "autocommit": False,
            "cursorclass": pymysql.cursors.DictCursor,
        }
        pool_size = int(os.getenv("IFUND_DB_POOL_SIZE", "8"))
        pool_timeout = float(os.getenv("IFUND_DB_POOL_TIMEOUT", "5"))
        self._pool = _MysqlConnectionPool(self._connect_kwargs, pool_size, pool_timeout)
        self._local = threading.local()
        atexit.register(self.close)

    @contextmanager
    def _connection(self) -> Iterator:
        """Use the active transaction connection or borrow one for this call."""
        transaction_connection = getattr(self._local, "transaction_connection", None)
        if transaction_connection is not None:
            yield transaction_connection
            return

        connection = self._pool.checkout()
        try:
            yield connection
        finally:
            self._pool.checkin(connection)

    def close(self) -> None:
        """Close the process connection pool at shutdown or teardown."""
        self._pool.close()

    def _in_transaction(self) -> bool:
        return getattr(self._local, "transaction_depth", 0) > 0

    @contextmanager
    def transaction(self) -> Iterator:
        """Run an atomic transaction, with savepoints for nested calls."""
        depth = getattr(self._local, "transaction_depth", 0)
        if depth:
            connection = self._local.transaction_connection
            savepoint = f"ifund_nested_{depth}"
            with connection.cursor() as cursor:
                cursor.execute(f"SAVEPOINT {savepoint}")
            self._local.transaction_depth = depth + 1
            try:
                yield connection
            except BaseException:
                with connection.cursor() as cursor:
                    cursor.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    cursor.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            else:
                with connection.cursor() as cursor:
                    cursor.execute(f"RELEASE SAVEPOINT {savepoint}")
            finally:
                self._local.transaction_depth = depth
            return

        connection = self._pool.checkout()
        self._local.transaction_connection = connection
        discard_connection = False
        try:
            try:
                connection.begin()
            except BaseException:
                discard_connection = True
                raise
            self._local.transaction_depth = 1
            try:
                yield connection
            except BaseException:
                try:
                    connection.rollback()
                except BaseException:
                    discard_connection = True
                    raise
                raise
            try:
                connection.commit()
            except BaseException:
                discard_connection = True
                raise
        finally:
            self._local.transaction_depth = 0
            self._local.transaction_connection = None
            self._pool.checkin(connection, discard=discard_connection)

    def select(self, table: str, params=None) -> list[dict]:
        _check_table(table)
        clauses = _build_clauses(params)
        sql = f"SELECT {clauses['select']} FROM `{table}`"
        if clauses["where"]:
            sql += " WHERE " + clauses["where"]
        if clauses["order"]:
            sql += " " + clauses["order"]
        if clauses["limit"] is not None:
            sql += " LIMIT %s"
        if clauses["offset"] is not None:
            sql += " OFFSET %s"
        query_params = list(clauses["where_params"])
        if clauses["limit"] is not None:
            query_params.append(clauses["limit"])
        if clauses["offset"] is not None:
            query_params.append(clauses["offset"])
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, query_params)
                rows = cursor.fetchall()
        return [dict(row) for row in (rows or [])]

    def insert(self, table: str, data: dict) -> dict:
        _check_table(table)
        if not data:
            raise ValueError("insert data 不能为空")
        columns = list(data)
        column_sql = ",".join(_quote_col(column) for column in columns)
        placeholders = ",".join(["%s"] * len(columns))
        sql = f"INSERT INTO `{table}` ({column_sql}) VALUES ({placeholders})"
        with self._connection() as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(sql, [data[column] for column in columns])
                    new_id = cursor.lastrowid
                if not self._in_transaction():
                    connection.commit()
            except pymysql.MySQLError as exc:
                if _is_duplicate_key(exc):
                    raise UniqueViolation(str(exc)) from exc
                raise

            if new_id:
                with connection.cursor() as cursor:
                    cursor.execute(f"SELECT * FROM `{table}` WHERE `id` = %s", [new_id])
                    row = cursor.fetchone()
                if row:
                    return dict(row)
                return {**data, "id": new_id}
            return dict(data)

    def batch_insert(self, table: str, rows: list[dict], batch_size: int = 500) -> None:
        _check_table(table)
        if not rows:
            return
        if batch_size <= 0:
            raise ValueError("batch_size 必须大于 0")
        columns = list(rows[0])
        if not columns:
            raise ValueError("batch_insert 行不能为空")
        column_sql = ",".join(_quote_col(column) for column in columns)
        placeholders = ",".join(["%s"] * len(columns))
        updates = ", ".join(
            f"{_quote_col(column)} = VALUES({_quote_col(column)})" for column in columns
        )
        sql = (
            f"INSERT INTO `{table}` ({column_sql}) VALUES ({placeholders}) "
            f"ON DUPLICATE KEY UPDATE {updates}"
        )
        with self._connection() as connection:
            try:
                for start in range(0, len(rows), batch_size):
                    chunk = rows[start : start + batch_size]
                    values = [[row.get(column) for column in columns] for row in chunk]
                    with connection.cursor() as cursor:
                        cursor.executemany(sql, values)
            except pymysql.MySQLError as exc:
                if _is_duplicate_key(exc):
                    raise UniqueViolation(str(exc)) from exc
                raise
            if not self._in_transaction():
                connection.commit()

    def update(self, table: str, filters: dict, data: dict) -> None:
        _check_table(table)
        if not data:
            raise ValueError("update data 不能为空")
        set_sql = ", ".join(f"{_quote_col(column)} = %s" for column in data)
        sql = f"UPDATE `{table}` SET {set_sql}"
        params = list(data.values())
        if filters:
            sql += " WHERE " + " AND ".join(
                f"{_quote_col(column)} = %s" for column in filters
            )
            params.extend(filters.values())
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
            if not self._in_transaction():
                connection.commit()

    def delete(self, table: str, filters: dict | None = None) -> None:
        _check_table(table)
        sql = f"DELETE FROM `{table}`"
        params: list = []
        if filters:
            sql += " WHERE " + " AND ".join(
                f"{_quote_col(column)} = %s" for column in filters
            )
            params.extend(filters.values())
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
            if not self._in_transaction():
                connection.commit()

    def count(self, table: str, params=None) -> int:
        _check_table(table)
        clauses = _build_clauses(params)
        sql = f"SELECT COUNT(*) AS n FROM `{table}`"
        if clauses["where"]:
            sql += " WHERE " + clauses["where"]
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, clauses["where_params"])
                row = cursor.fetchone()
        return int(_row_value(row, "n", 0) or 0)

    @staticmethod
    def _build_join_order(order_parts) -> str:
        if not order_parts:
            return "ORDER BY `f`.`code` ASC"
        segments = []
        for field, direction in order_parts:
            sql_direction = "DESC" if str(direction).lower() == "desc" else "ASC"
            if field in SORTABLE_AI:
                alias = "a"
            elif field in SORTABLE_DETAIL:
                alias = "d"
            else:
                alias = "f"
            segments.append(f"{_quote_col(f'{alias}.{field}')} {sql_direction}")
        return "ORDER BY " + ", ".join(segments)

    def list_funds_with_details(
        self, fund_params, detail_params, skip, limit, order_parts
    ):
        fund_clauses = _build_clauses(fund_params, "f")
        detail_clauses = _build_clauses(detail_params, "d")
        where_parts: list[str] = []
        where_params: list = []
        for clauses in (fund_clauses, detail_clauses):
            if clauses["where"]:
                where_parts.append(clauses["where"])
                where_params.extend(clauses["where_params"])
        where_sql = " WHERE " + " AND ".join(where_parts) if where_parts else ""
        base = (
            "FROM `funds` f "
            "LEFT JOIN `fund_details` d ON f.`code` = d.`fund_code` "
            "LEFT JOIN `fund_ai_analysis` a ON f.`code` = a.`fund_code`"
        )
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT COUNT(*) AS n {base}{where_sql}", where_params)
                total = int(_row_value(cursor.fetchone(), "n", 0) or 0)
                order_sql = self._build_join_order(order_parts)
                sql = (
                    f"SELECT {', '.join(_RESULT_COLS)} {base}{where_sql} {order_sql} "
                    "LIMIT %s OFFSET %s"
                )
                cursor.execute(sql, where_params + [limit, skip])
                rows = cursor.fetchall()
        return total, [dict(row) for row in (rows or [])]

    def list_industry_mapping(
        self, *, market="", label_kw="", status="", keyword="", skip=0, limit=50
    ):
        base = """
            WITH held AS (
                SELECT `asset_code` AS `stock_code`, MIN(`asset_name`) AS `held_name`
                FROM `fund_holdings` WHERE `holding_type` = 'stock' GROUP BY `asset_code`
            ),
            m AS (
                SELECT h.`stock_code`,
                    COALESCE(NULLIF(si.`stock_name`, ''), h.`held_name`, '') AS `stock_name`,
                    COALESCE(NULLIF(si.`market`, ''),
                        CASE
                            WHEN h.`stock_code` REGEXP '^[0-9]{6}$' THEN 'A'
                            WHEN h.`stock_code` REGEXP '^[0-9]{5}$' THEN 'HK'
                            ELSE 'OTHER'
                        END) AS `market`,
                    COALESCE(si.`sw_l1`, '') AS `sw_l1`,
                    COALESCE(si.`sw_l2`, '') AS `sw_l2`,
                    COALESCE(si.`sw_l3`, '') AS `sw_l3`,
                    COALESCE(si.`em_industry`, '') AS `em_industry`,
                    COALESCE(si.`source`, '') AS `source`,
                    COALESCE(si.`manual`, 0) AS `manual`,
                    CASE WHEN COALESCE(si.`sw_l3`, '') <> ''
                              OR COALESCE(si.`em_industry`, '') <> ''
                         THEN 1 ELSE 0 END AS `covered`
                FROM held h LEFT JOIN `stock_industry` si ON si.`stock_code` = h.`stock_code`
            ),
            r AS (
                SELECT *,
                    CASE WHEN `covered` = 1
                         THEN COALESCE(NULLIF(`sw_l3`, ''), NULLIF(`sw_l2`, ''),
                                       NULLIF(`em_industry`, ''))
                         ELSE '' END AS `label`
                FROM m
            )
        """
        where = ["1 = 1"]
        params: list = []
        if market:
            where.append("`market` = %s")
            params.append(market)
        if status == "covered":
            where.append("`covered` = 1")
        elif status == "uncovered":
            where.append("`covered` = 0")
        if label_kw:
            where.append("CONCAT(`sw_l3`, `sw_l2`, `sw_l1`, `em_industry`) LIKE %s")
            params.append(f"%{label_kw}%")
        if keyword:
            where.append("(`stock_code` LIKE %s OR `stock_name` LIKE %s)")
            params.extend([f"%{keyword}%", f"%{keyword}%"])
        where_sql = " WHERE " + " AND ".join(where)
        sql = (
            f"{base} SELECT *, COUNT(*) OVER () AS _total FROM r{where_sql} "
            "ORDER BY `covered` DESC, `stock_code` ASC LIMIT %s OFFSET %s"
        )
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, params + [limit, skip])
                rows = [dict(row) for row in (cursor.fetchall() or [])]
        total = int(rows[0].pop("_total") or 0) if rows else 0
        for row in rows:
            row.pop("_total", None)
        return total, rows

    def list_manager_summary(
        self,
        *,
        keyword="",
        coverage="all",
        preset_id=None,
        skip=0,
        limit=50,
        order_field="code",
        order_dir="asc",
    ):
        base = (
            "FROM `funds` f "
            "LEFT JOIN `fund_details` d ON f.`code` = d.`fund_code` "
            "LEFT JOIN `fund_manager_tenure` t "
            "ON f.`code` = t.`fund_code` AND t.`seq` = 0 AND t.`is_current` = 1"
        )
        where = ["1 = 1"]
        params: list = []
        if preset_id:
            # JSON_SEARCH replaces SQLite's json_each/json_extract pair while
            # avoiding a generated numbers table for the JSON array.
            where.append(
                "EXISTS (SELECT 1 FROM `fund_snapshots` s "
                "WHERE s.`preset_id` = %s "
                "AND JSON_SEARCH(s.`items_json`, 'one', f.`code`, NULL, '$[*].code') IS NOT NULL)"
            )
            params.append(preset_id)
        if keyword:
            where.append(
                "(f.`code` LIKE %s OR d.`fund_name` LIKE %s "
                "OR d.`fund_manager` LIKE %s OR t.`managers` LIKE %s)"
            )
            keyword_pattern = f"%{keyword}%"
            params.extend([keyword_pattern] * 4)
        if coverage == "covered":
            where.append("t.`fund_code` IS NOT NULL")
        elif coverage == "uncovered":
            where.append("t.`fund_code` IS NULL")
        where_sql = " WHERE " + " AND ".join(where)
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT COUNT(*) AS n {base}{where_sql}", params)
                total = int(_row_value(cursor.fetchone(), "n", 0) or 0)
                alias = _MGR_SORTABLE.get(order_field, "f")
                column = order_field if order_field in _MGR_SORTABLE else "code"
                sql_direction = "DESC" if str(order_dir).lower() == "desc" else "ASC"
                order_sql = (
                    f"ORDER BY {_quote_col(f'{alias}.{column}')} {sql_direction}"
                )
                select_cols = (
                    "f.`code` AS code, f.`name` AS name, "
                    "d.`fund_type`, d.`fund_company`, d.`scale`, d.`fund_manager`, "
                    "d.`return_1y`, d.`return_3y`, "
                    "t.`managers`, t.`start_date`, t.`end_date`, t.`tenure_text`, "
                    "t.`tenure_days`, t.`tenure_return`, t.`fetch_time`"
                )
                sql = (
                    f"SELECT {select_cols} {base}{where_sql} {order_sql} "
                    "LIMIT %s OFFSET %s"
                )
                cursor.execute(sql, params + [limit, skip])
                rows = cursor.fetchall()
        return total, [dict(row) for row in (rows or [])]

    def init_db(self, schema_sql: str) -> None:
        statements = _convert_schema_sql(schema_sql)
        if not statements:
            return
        with self._connection() as connection:
            with connection.cursor() as cursor:
                for statement in statements:
                    try:
                        cursor.execute(statement)
                    except pymysql.MySQLError as exc:
                        if _is_duplicate_index(exc):
                            continue
                        raise
            connection.commit()
