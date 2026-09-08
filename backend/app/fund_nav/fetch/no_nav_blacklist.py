"""无单位净值基金的独立持久化状态库。"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from app.fund_nav.fetch import errors

DEFAULT_PATH = Path(__file__).resolve().parents[3] / "nav_blacklist.db"
TABLE = "fund_nav_blacklist"
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    fund_code TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    source TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    evidence_count INTEGER NOT NULL
)
"""


def _path(path: str | os.PathLike[str] | None = None) -> Path:
    return Path(path or os.getenv("IFUND_NAV_BLACKLIST_PATH") or DEFAULT_PATH)


def _connect(path: Path, *, create: bool) -> sqlite3.Connection | None:
    if not create and not path.exists():
        return None
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5)
    connection.row_factory = sqlite3.Row
    if create:
        connection.execute(_SCHEMA)
        connection.commit()
    return connection


def _not_expired(value: object) -> bool:
    """兼容历史无时区时间与新带时区时间。"""
    if not value:
        return True
    expiry = dt.datetime.fromisoformat(str(value))
    current = dt.datetime.now().astimezone()
    if expiry.tzinfo is None:
        current = current.replace(tzinfo=None)
    return expiry > current


def get_record(
    fund_code: str, *, path: str | os.PathLike[str] | None = None
) -> dict | None:
    """查询单只基金的黑名单记录；状态文件尚未创建时直接未命中。"""
    connection = _connect(_path(path), create=False)
    if connection is None:
        return None
    try:
        try:
            columns = {
                item[1]
                for item in connection.execute(f"PRAGMA table_info({TABLE})").fetchall()
            }
            selected = "fund_code,reason,detected_at"
            if {"source", "expires_at", "evidence_count"}.issubset(columns):
                selected += ",source,expires_at,evidence_count"
            row = connection.execute(
                f"SELECT {selected} FROM {TABLE} WHERE fund_code = ?",
                (str(fund_code).strip(),),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        return dict(row) if row else None
    finally:
        connection.close()


def is_blacklisted(
    fund_code: str, *, path: str | os.PathLike[str] | None = None
) -> bool:
    """仅命中尚未到期的多源确认记录；旧结构在迁移前保持兼容。"""
    record_value = get_record(fund_code, path=path)
    if record_value is None:
        return False
    return _not_expired(record_value.get("expires_at"))


def record(
    fund_code: str,
    reason: str,
    *,
    sources: Iterable[str],
    detected_at: str | None = None,
    expires_at: str | None = None,
    path: str | os.PathLike[str] | None = None,
) -> dict:
    """仅在至少两个独立来源确认后写入带到期日的基金级黑名单。"""
    code = str(fund_code).strip()
    if not code:
        raise ValueError("fund_code must not be empty")
    source_list = sorted(
        {str(source).strip() for source in sources if str(source).strip()}
    )
    if len(source_list) < 2:
        raise ValueError("blacklist requires confirmation from at least two sources")
    timestamp = detected_at or dt.datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    expiry = expires_at or (
        dt.datetime.now().astimezone() + dt.timedelta(days=30)
    ).isoformat(timespec="seconds")
    connection = _connect(_path(path), create=True)
    assert connection is not None
    try:
        connection.execute(
            f"""
            INSERT INTO {TABLE}
                (fund_code, reason, detected_at, source, expires_at, evidence_count)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(fund_code) DO UPDATE SET
                reason = excluded.reason,
                detected_at = excluded.detected_at,
                source = excluded.source,
                expires_at = excluded.expires_at,
                evidence_count = excluded.evidence_count
            """,
            (
                code,
                str(reason),
                timestamp,
                ",".join(source_list),
                expiry,
                len(source_list),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return {
        "fund_code": code,
        "reason": str(reason),
        "detected_at": timestamp,
        "source": ",".join(source_list),
        "expires_at": expiry,
        "evidence_count": len(source_list),
    }


def list_records(*, path: str | os.PathLike[str] | None = None) -> list[dict]:
    """查询全部黑名单记录，按检测时间和代码排序。"""
    connection = _connect(_path(path), create=False)
    if connection is None:
        return []
    try:
        try:
            columns = {
                item[1]
                for item in connection.execute(f"PRAGMA table_info({TABLE})").fetchall()
            }
            selected = "fund_code,reason,detected_at"
            if {"source", "expires_at", "evidence_count"}.issubset(columns):
                selected += ",source,expires_at,evidence_count"
            rows = connection.execute(
                f"SELECT {selected} FROM {TABLE} ORDER BY detected_at,fund_code"
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(row) for row in rows]
    finally:
        connection.close()


def blacklisted_codes(*, path: str | os.PathLike[str] | None = None) -> set[str]:
    """批处理入口一次性读取代码集合，避免逐基金重复打开状态库。"""
    return {
        row["fund_code"]
        for row in list_records(path=path)
        if _not_expired(row.get("expires_at"))
    }


def record_rank_snapshot_missing(
    fund_code: str,
    *,
    confirming_sources: Iterable[str],
    path: str | os.PathLike[str] | None = None,
) -> dict:
    """rank 快照缺失仍需另一独立来源确认，不能单源永久隔离。"""
    return record(
        fund_code,
        errors.RANK_SNAPSHOT_MISSING,
        sources=["rank_snapshot", *confirming_sources],
        path=path,
    )
