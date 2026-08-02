"""SQLite 后端：把统一过滤语法（PostgREST 风格 DSL）翻译成 SQL 并执行。

支持的过滤语法（``params`` 为 dict 或 list[tuple(key, val)]）：

| 语法                              | SQL 等价                         |
|-----------------------------------|----------------------------------|
| ``("col", "eq.v")``               | ``col = ?``                      |
| ``("col", "neq.v")``              | ``col != ?``                     |
| ``("col", "gt.v")`` / ``gte.``    | ``col > ?`` / ``>=``             |
| ``("col", "lt.v")`` / ``lte.``    | ``col < ?`` / ``<=``             |
| ``("col", "ilike.*kw*")``         | ``col LIKE ? COLLATE NOCASE``    |
| ``("col", "not.ilike.*kw*")``     | ``col NOT LIKE ? COLLATE NOCASE``|
| ``("col", "in.(a,b,c)")``         | ``col IN (?,?,?)``               |
| ``("col", "not.in.(a,b,c)")``     | ``col NOT IN (?,?,?)``           |
| ``("or", "(c1.eq.a,c2.ilike.*b*)")`` | ``(c1 = ? OR c2 LIKE ?)``     |
| ``("select", "a,b,c")``           | ``SELECT a,b,c``                 |
| ``("order", "col.desc,col2.asc")``| ``ORDER BY col DESC, col2 ASC``  |
| ``("limit", n)`` / ``("offset", n)`` | ``LIMIT n`` / ``OFFSET n``    |

所有值走参数化绑定（防注入）。
"""
from __future__ import annotations

import atexit
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from .base import Database, UniqueViolation

# ── 表名白名单：仅允许 schema_sqlite.sql 中已定义的表。新增表时同步更新本清单。 ──
VALID_TABLES: frozenset[str] = frozenset({
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
    "fund_cum_return",
    "fund_manager_tenure",
    "stock_industry",
    "portfolios",
    "user_holdings",
    "holding_txns",
    "fund_ai_analysis",
    "app_settings",
    "perpetual_portfolio",
})


def _check_table(table: str) -> None:
    """表名白名单校验：不在 VALID_TABLES 中则拒绝，防止 SQL 注入。"""
    if table not in VALID_TABLES:
        raise ValueError(f"非法表名: {table}")


# 可按 fund_details 列排序的白名单（list_funds_with_details 用）
SORTABLE_DETAIL = {
    "scale", "return_ytd", "drawdown_ytd", "sharpe_3y", "sharpe_1y",
    "max_drawdown_3y", "max_drawdown_1y", "position_stock",
}
# AI 定性分析可排序列（fund_ai_analysis，别名 a）
SORTABLE_AI = {"skill_score", "rating", "tenure_years"}

# 联合查询返回列（两后端结构必须一致）
_RESULT_COLS = [
    'f."id" AS id', 'f."code" AS code', 'f."name" AS name', 'f."type" AS type',
    'f."fund_type" AS fund_type', 'd."fund_manager" AS fund_manager', 'd."scale" AS scale',
    'd."sharpe_3y" AS sharpe_3y', 'd."sharpe_1y" AS sharpe_1y',
    'd."max_drawdown_3y" AS max_drawdown_3y', 'd."max_drawdown_1y" AS max_drawdown_1y',
    'd."position_stock" AS position_stock', 'd."position_bond" AS position_bond',
    'd."return_ytd" AS return_ytd', 'd."drawdown_ytd" AS drawdown_ytd',
]

# OR 子句切分：逗号后须跟「非括号字符直到 ( 或 字符串结尾」，避免切到 in.(a,b) 内部
_OR_SPLIT_RE = re.compile(r",(?=[^()]*(?:\(|$))")


def _quote_col(col: str) -> str:
    """处理 ``table.field`` 形式，逐段加双引号。"""
    return ".".join(f'"{part}"' for part in col.split("."))


def _prefixed(col: str, prefix: str) -> str:
    """给无前缀的列名补上表别名前缀（JOIN 用）；已含 ``.`` 的不动。"""
    if not prefix or "." in col:
        return col
    return f"{prefix}.{col}"


def _parse_in_list(raw: str) -> list[str]:
    """``(a,b,c)`` → ``["a","b","c"]``。"""
    raw = raw.strip()
    if raw.startswith("(") and raw.endswith(")"):
        raw = raw[1:-1]
    return [x.strip() for x in raw.split(",") if x.strip() != ""]


def _in_clause(col: str, raw: str, negate: bool):
    items = _parse_in_list(raw)
    placeholders = ",".join("?" * len(items)) or "NULL"
    op = "NOT IN" if negate else "IN"
    return f"{col} {op} ({placeholders})", items


# 操作符前缀 → SQL 片段构造器。顺序重要：长前缀/取反在前。
_OPERATORS = [
    ("not.ilike.", lambda c, v: (f"{c} NOT LIKE ? COLLATE NOCASE", [v.replace("*", "%")])),
    ("ilike.", lambda c, v: (f"{c} LIKE ? COLLATE NOCASE", [v.replace("*", "%")])),
    ("not.in.", lambda c, v: _in_clause(c, v, True)),
    ("in.", lambda c, v: _in_clause(c, v, False)),
    ("neq.", lambda c, v: (f"{c} != ?", [v])),
    ("eq.", lambda c, v: (f"{c} = ?", [v])),
    ("gte.", lambda c, v: (f"{c} >= ?", [v])),
    ("gt.", lambda c, v: (f"{c} > ?", [v])),
    ("lte.", lambda c, v: (f"{c} <= ?", [v])),
    ("lt.", lambda c, v: (f"{c} < ?", [v])),
]


def _parse_filter(col: str, val) -> tuple[str, list]:
    """单列条件 → ``(sql, params)``，无前缀匹配则按等值。"""
    qcol = _quote_col(col)
    s = str(val)
    for prefix, builder in _OPERATORS:
        if s.startswith(prefix):
            return builder(qcol, s[len(prefix):])
    return f"{qcol} = ?", [s]


def _parse_or(val, prefix: str = "") -> tuple[str, list]:
    """``(c1.eq.a,c2.ilike.*b*)`` → ``(c1 = ? OR c2 LIKE ?)``。"""
    inner = str(val).strip()
    if inner.startswith("(") and inner.endswith(")"):
        inner = inner[1:-1]
    parts: list[str] = []
    params: list = []
    for clause in _OR_SPLIT_RE.split(inner):
        clause = clause.strip()
        if not clause:
            continue
        col, _, rest = clause.partition(".")
        sql, ps = _parse_filter(_prefixed(col, prefix), rest)
        parts.append(sql)
        params.extend(ps)
    return "(" + " OR ".join(parts) + ")", params


def _build_order(val, prefix: str = "") -> str:
    segs = []
    for seg in str(val).split(","):
        seg = seg.strip()
        if not seg:
            continue
        if seg.endswith(".desc"):
            col, direction = seg[:-5], "DESC"
        elif seg.endswith(".asc"):
            col, direction = seg[:-4], "ASC"
        else:
            col, direction = seg, "ASC"
        segs.append(f"{_quote_col(_prefixed(col, prefix))} {direction}")
    return ("ORDER BY " + ", ".join(segs)) if segs else ""


def _normalize_params(params):
    if params is None:
        return []
    if isinstance(params, dict):
        return list(params.items())
    return list(params)


def _build_clauses(params, prefix: str = "") -> dict:
    """把过滤语法拆成 where / order / limit / offset / select 各部分。"""
    where_parts: list[str] = []
    where_params: list = []
    order, select = "", "*"
    limit = offset = None
    for key, val in _normalize_params(params):
        if key == "select":
            select = str(val)
        elif key == "order":
            order = _build_order(val, prefix)
        elif key == "limit":
            limit = int(val)
        elif key == "offset":
            offset = int(val)
        elif key == "or":
            sql, ps = _parse_or(val, prefix)
            where_parts.append(sql)
            where_params.extend(ps)
        else:
            sql, ps = _parse_filter(_prefixed(key, prefix), val)
            where_parts.append(sql)
            where_params.extend(ps)
    return {
        "where": " AND ".join(where_parts),
        "where_params": where_params,
        "order": order,
        "limit": limit,
        "offset": offset,
        "select": select,
    }


class SqliteDatabase(Database):
    """SQLite 实现：每线程一个连接（WAL），用于 Flask 多线程与 worker 线程池。"""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        atexit.register(self._close_connections)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            with self._connections_lock:
                self._connections.append(conn)
            self._local.conn = conn
        return conn

    def _close_connections(self) -> None:
        with self._connections_lock:
            connections = self._connections
            self._connections = []
        for conn in connections:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def _in_transaction(self) -> bool:
        return getattr(self._local, "transaction_depth", 0) > 0

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """在当前线程的连接上执行原子事务。

        使用 ``BEGIN IMMEDIATE`` 在读-改-写前取得 SQLite 写锁，配合连接上的
        ``busy_timeout`` 串行化并发写事务；嵌套调用使用 savepoint。事务内的
        CRUD 方法不会自行提交，异常时由这里统一回滚。
        """
        conn = self._conn()
        depth = getattr(self._local, "transaction_depth", 0)
        if depth:
            savepoint = f"sqlite_nested_{depth}"
            conn.execute(f"SAVEPOINT {savepoint}")
            self._local.transaction_depth = depth + 1
            try:
                yield conn
            except BaseException:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            else:
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            finally:
                self._local.transaction_depth = depth
            return

        conn.execute("BEGIN IMMEDIATE")
        self._local.transaction_depth = 1
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
        finally:
            self._local.transaction_depth = 0

    def select(self, table: str, params=None) -> list[dict]:
        _check_table(table)
        c = _build_clauses(params)
        sql = f'SELECT {c["select"]} FROM "{table}"'
        if c["where"]:
            sql += " WHERE " + c["where"]
        if c["order"]:
            sql += " " + c["order"]
        if c["limit"] is not None:
            sql += f' LIMIT {c["limit"]}'
        if c["offset"] is not None:
            sql += f' OFFSET {c["offset"]}'
        cur = self._conn().execute(sql, c["where_params"])
        return [dict(row) for row in cur.fetchall()]

    def insert(self, table: str, data: dict) -> dict:
        _check_table(table)
        cols = list(data.keys())
        col_sql = ",".join(f'"{col}"' for col in cols)
        placeholders = ",".join("?" * len(cols))
        conn = self._conn()
        try:
            cur = conn.execute(
                f'INSERT INTO "{table}" ({col_sql}) VALUES ({placeholders})',
                [data[col] for col in cols],
            )
            if not self._in_transaction():
                conn.commit()
        except sqlite3.IntegrityError as exc:
            if not self._in_transaction():
                conn.rollback()
            raise UniqueViolation(str(exc)) from exc
        row = conn.execute(
            f'SELECT * FROM "{table}" WHERE rowid = ?', [cur.lastrowid]
        ).fetchone()
        return dict(row) if row else {**data, "id": cur.lastrowid}

    def batch_insert(self, table: str, rows: list[dict], batch_size: int = 500) -> None:
        _check_table(table)
        if not rows:
            return
        cols = list(rows[0].keys())
        col_sql = ",".join(f'"{col}"' for col in cols)
        placeholders = ",".join("?" * len(cols))
        sql = f'INSERT OR REPLACE INTO "{table}" ({col_sql}) VALUES ({placeholders})'
        conn = self._conn()
        try:
            for i in range(0, len(rows), batch_size):
                chunk = rows[i:i + batch_size]
                conn.executemany(sql, [[row.get(col) for col in cols] for row in chunk])
        except BaseException:
            if not self._in_transaction():
                conn.rollback()
            raise
        if not self._in_transaction():
            conn.commit()

    def update(self, table: str, filters: dict, data: dict) -> None:
        _check_table(table)
        set_sql = ", ".join(f'"{col}" = ?' for col in data)
        sql = f'UPDATE "{table}" SET {set_sql}'
        params = list(data.values())
        if filters:
            sql += " WHERE " + " AND ".join(f'"{col}" = ?' for col in filters)
            params += list(filters.values())
        conn = self._conn()
        try:
            conn.execute(sql, params)
        except BaseException:
            if not self._in_transaction():
                conn.rollback()
            raise
        if not self._in_transaction():
            conn.commit()

    def delete(self, table: str, filters: dict | None = None) -> None:
        _check_table(table)
        sql = f'DELETE FROM "{table}"'
        params: list = []
        if filters:
            sql += " WHERE " + " AND ".join(f'"{col}" = ?' for col in filters)
            params = list(filters.values())
        conn = self._conn()
        try:
            conn.execute(sql, params)
        except BaseException:
            if not self._in_transaction():
                conn.rollback()
            raise
        if not self._in_transaction():
            conn.commit()

    def count(self, table: str, params=None) -> int:
        _check_table(table)
        c = _build_clauses(params)
        sql = f'SELECT COUNT(*) AS n FROM "{table}"'
        if c["where"]:
            sql += " WHERE " + c["where"]
        cur = self._conn().execute(sql, c["where_params"])
        return int(cur.fetchone()["n"])

    @staticmethod
    def _build_join_order(order_parts) -> str:
        if not order_parts:
            return 'ORDER BY f."code" ASC'
        segs = []
        for field, direction in order_parts:
            sql_dir = "DESC" if str(direction).lower() == "desc" else "ASC"
            if field in SORTABLE_AI:
                alias = "a"
            elif field in SORTABLE_DETAIL:
                alias = "d"
            else:
                alias = "f"
            segs.append(f'{alias}."{field}" {sql_dir}')
        return "ORDER BY " + ", ".join(segs)

    def list_funds_with_details(self, fund_params, detail_params, skip, limit, order_parts):
        fund_c = _build_clauses(fund_params, "f")
        detail_c = _build_clauses(detail_params, "d")
        where_parts, where_params = [], []
        for clause in (fund_c, detail_c):
            if clause["where"]:
                where_parts.append(clause["where"])
                where_params.extend(clause["where_params"])
        where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
        # base 是模块内硬编码的三表 JOIN，表名来自 schema 定义，非用户输入；
        # 三表名均已在 VALID_TABLES 白名单中。
        base = ('FROM "funds" f '
                'LEFT JOIN "fund_details" d ON f."code" = d."fund_code" '
                'LEFT JOIN "fund_ai_analysis" a ON f."code" = a."fund_code"')
        conn = self._conn()
        total = int(
            conn.execute(f"SELECT COUNT(*) AS n {base}{where_sql}", where_params).fetchone()["n"]
        )
        order_sql = self._build_join_order(order_parts)
        sql = (
            f'SELECT {", ".join(_RESULT_COLS)} {base}{where_sql} {order_sql} LIMIT ? OFFSET ?'
        )
        rows = conn.execute(sql, where_params + [limit, skip]).fetchall()
        return total, [dict(row) for row in rows]

    def list_industry_mapping(self, *, market="", label_kw="", status="", keyword="", skip=0, limit=50):
        # held：持仓股票去重（带簡称），走 (holding_type, asset_code, asset_name) 覆盖索引，免全表扫。
        # m：LEFT JOIN 行业映射后派生 market（缺映射按代码形态兜底）/ covered（有申万三级或东财）。
        # 末层再算 label（覆盖时取 申万三级→二级→东财），并把过滤/排序/分页全交给 SQL。
        # base 是模块内硬编码的 CTE 查询，所有表名来自 schema 定义，非用户输入。
        base = """
            WITH held AS (
                SELECT asset_code AS stock_code, MIN(asset_name) AS held_name
                FROM fund_holdings WHERE holding_type = 'stock' GROUP BY asset_code
            ),
            m AS (
                SELECT h.stock_code,
                    COALESCE(NULLIF(si.stock_name, ''), h.held_name, '') AS stock_name,
                    COALESCE(NULLIF(si.market, ''),
                        CASE
                            WHEN h.stock_code GLOB '[0-9][0-9][0-9][0-9][0-9][0-9]' THEN 'A'
                            WHEN h.stock_code GLOB '[0-9][0-9][0-9][0-9][0-9]' THEN 'HK'
                            ELSE 'OTHER'
                        END) AS market,
                    COALESCE(si.sw_l1, '') AS sw_l1,
                    COALESCE(si.sw_l2, '') AS sw_l2,
                    COALESCE(si.sw_l3, '') AS sw_l3,
                    COALESCE(si.em_industry, '') AS em_industry,
                    COALESCE(si.source, '') AS source,
                    COALESCE(si.manual, 0) AS manual,
                    CASE WHEN COALESCE(si.sw_l3, '') <> '' OR COALESCE(si.em_industry, '') <> ''
                         THEN 1 ELSE 0 END AS covered
                FROM held h LEFT JOIN stock_industry si ON si.stock_code = h.stock_code
            ),
            r AS (
                SELECT *,
                    CASE WHEN covered = 1
                         THEN COALESCE(NULLIF(sw_l3, ''), NULLIF(sw_l2, ''), NULLIF(em_industry, ''))
                         ELSE '' END AS label
                FROM m
            )
        """
        where, params = ["1 = 1"], []
        if market:
            where.append("market = ?")
            params.append(market)
        if status == "covered":
            where.append("covered = 1")
        elif status == "uncovered":
            where.append("covered = 0")
        if label_kw:
            where.append("(sw_l3 || sw_l2 || sw_l1 || em_industry) LIKE ?")
            params.append(f"%{label_kw}%")
        if keyword:
            where.append("(stock_code LIKE ? OR stock_name LIKE ?)")
            params.extend([f"%{keyword}%", f"%{keyword}%"])
        where_sql = " WHERE " + " AND ".join(where)
        # COUNT(*) OVER () 随行带回过滤后总数，重 CTE+JOIN 只跑一遍（免去单独 COUNT 查询）。
        sql = (
            f"{base} SELECT *, COUNT(*) OVER () AS _total FROM r{where_sql} "
            "ORDER BY covered DESC, stock_code ASC LIMIT ? OFFSET ?"
        )
        rows = [dict(row) for row in self._conn().execute(sql, params + [limit, skip]).fetchall()]
        total = rows[0].pop("_total") if rows else 0
        for r in rows:
            r.pop("_total", None)
        return total, rows

    _MGR_SORTABLE = {
        "code": "f", "name": "f",
        "fund_type": "d", "fund_company": "d", "scale": "d", "fund_manager": "d",
        "return_1y": "d", "return_3y": "d",
        "managers": "t", "tenure_days": "t", "tenure_return": "t", "start_date": "t",
    }

    def list_manager_summary(self, *, keyword="", coverage="all", preset_id=None,
                             skip=0, limit=50, order_field="code", order_dir="asc"):
        # base 是模块内硬编码的三表 JOIN，表名来自 schema 定义，非用户输入。
        base = ('FROM "funds" f '
                'LEFT JOIN "fund_details" d ON f."code" = d."fund_code" '
                'LEFT JOIN "fund_manager_tenure" t '
                'ON f."code" = t."fund_code" AND t."seq" = 0 AND t."is_current" = 1')
        where, params = ["1 = 1"], []
        if preset_id:
            where.append(('f."code" IN (SELECT json_extract(value,\'$.code\') '
                          'FROM "fund_snapshots", json_each("items_json") '
                          'WHERE "preset_id" = ?)'))
            params.append(preset_id)
        if keyword:
            where.append('(f."code" LIKE ? OR d."fund_name" LIKE ? '
                         'OR d."fund_manager" LIKE ? OR t."managers" LIKE ?)')
            kw = f"%{keyword}%"
            params.extend([kw, kw, kw, kw])
        if coverage == "covered":
            where.append('t."fund_code" IS NOT NULL')
        elif coverage == "uncovered":
            where.append('t."fund_code" IS NULL')
        where_sql = " WHERE " + " AND ".join(where)
        conn = self._conn()
        total = int(
            conn.execute(f"SELECT COUNT(*) AS n {base}{where_sql}", params).fetchone()["n"]
        )
        alias = self._MGR_SORTABLE.get(order_field, "f")
        col = order_field if order_field in self._MGR_SORTABLE else "code"
        sql_dir = "DESC" if order_dir.lower() == "desc" else "ASC"
        order_sql = f'ORDER BY {alias}."{col}" {sql_dir}'
        select_cols = ('f."code" AS code, f."name" AS name, '
                       'd."fund_type", d."fund_company", d."scale", d."fund_manager", '
                       'd."return_1y", d."return_3y", '
                       't."managers", t."start_date", t."end_date", t."tenure_text", '
                       't."tenure_days", t."tenure_return", t."fetch_time"')
        sql = f"SELECT {select_cols} {base}{where_sql} {order_sql} LIMIT ? OFFSET ?"
        rows = conn.execute(sql, params + [limit, skip]).fetchall()
        return total, [dict(row) for row in rows]

    def init_db(self, schema_sql: str) -> None:
        conn = self._conn()
        conn.executescript(schema_sql)
        conn.commit()
