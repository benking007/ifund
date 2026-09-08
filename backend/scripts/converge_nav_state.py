#!/usr/bin/env python3
"""一次性收敛净值黑名单、adj repair 和事实水位，并保存可回滚证据。"""

from __future__ import annotations

# 运维脚本刻意展开备份、核验和分批写入步骤，便于事故审计。
# pylint: disable=too-many-locals,too-many-statements,too-many-branches
import argparse
import datetime as dt
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)

from app.fund_nav.daily_sync import (  # pylint: disable=wrong-import-position
    atomic_json,
    connect_mysql,
)

HOWBUY_REASON = "howbuy_redirect_code_invalid"
REPAIR_CUTOFF = "2026-08-31"
CHUNK_SIZE = 500


def timestamp() -> str:
    """生成表名和证据共用的稳定时间戳。"""
    return dt.datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")


def chunks(values: list, size: int = CHUNK_SIZE):
    """按固定大小切片。"""
    for start in range(0, len(values), size):
        yield values[start : start + size]


def inspect_blacklist(path: Path) -> dict:
    """只读统计旧 blacklist。"""
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        columns = [
            row[1]
            for row in connection.execute("PRAGMA table_info(fund_nav_blacklist)")
        ]
        rows = connection.execute(
            "SELECT reason,COUNT(*) n FROM fund_nav_blacklist GROUP BY reason ORDER BY n DESC"
        ).fetchall()
        howbuy = connection.execute(
            "SELECT COUNT(*) FROM fund_nav_blacklist WHERE reason=?", (HOWBUY_REASON,)
        ).fetchone()[0]
        return {
            "path": str(path),
            "columns": columns,
            "total": sum(int(row["n"]) for row in rows),
            "howbuy_redirect": int(howbuy),
            "by_reason": {str(row["reason"]): int(row["n"]) for row in rows},
        }
    finally:
        connection.close()


def converge_blacklist(path: Path, backup_dir: Path, stamp: str, dry_run: bool) -> dict:
    """备份后仅删除 Howbuy 302 误判，并给保留记录补来源/到期字段。"""
    before = inspect_blacklist(path)
    backup_path = backup_dir / f"nav_blacklist.db.{stamp}.bak"
    if dry_run:
        return {
            "before": before,
            "would_remove": before["howbuy_redirect"],
            "backup": None,
        }
    backup_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup_path)
    connection = sqlite3.connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(fund_nav_blacklist)")
        }
        additions = {
            "source": "TEXT NOT NULL DEFAULT 'legacy_single_source'",
            "expires_at": "TEXT",
            "evidence_count": "INTEGER NOT NULL DEFAULT 1",
        }
        for column, definition in additions.items():
            if column not in columns:
                connection.execute(
                    f"ALTER TABLE fund_nav_blacklist ADD COLUMN {column} {definition}"
                )
        cursor = connection.execute(
            "DELETE FROM fund_nav_blacklist WHERE reason=?", (HOWBUY_REASON,)
        )
        removed = int(cursor.rowcount)
        expiry = (dt.datetime.now().astimezone() + dt.timedelta(days=30)).isoformat(
            timespec="seconds"
        )
        connection.execute(
            "UPDATE fund_nav_blacklist SET "
            "source=CASE WHEN reason LIKE 'akshare_%' THEN 'akshare_legacy' "
            "ELSE 'legacy_single_source' END,"
            "expires_at=COALESCE(expires_at,?),evidence_count=MAX(evidence_count,1)",
            (expiry,),
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    after = inspect_blacklist(path)
    if removed != before["howbuy_redirect"] or after["howbuy_redirect"] != 0:
        raise RuntimeError("blacklist 清理后核验失败")
    return {
        "before": before,
        "after": after,
        "removed": removed,
        "retained": after["total"],
        "retained_expiry": expiry,
        "backup": str(backup_path),
        "rollback": f"停止同步后用 {backup_path} 覆盖 {path}",
    }


def load_repair_selection(connection) -> tuple[list[int], dict[str, str]]:
    """只选择 cutoff 前且事实 latest_adj 已覆盖 gap_end 的 failed adj。"""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT id,fund_code,gap_end FROM nav_repair_queue "
            "WHERE task_kind='adj' AND status='failed' AND gap_end<=%s",
            (REPAIR_CUTOFF,),
        )
        repairs = cursor.fetchall()
        cursor.execute(
            "SELECT fund_code,MAX(trade_date) latest_adj FROM fund_nav "
            "WHERE adj_nav IS NOT NULL GROUP BY fund_code"
        )
        latest_adj = {
            str(row["fund_code"]): str(row["latest_adj"]) for row in cursor.fetchall()
        }
    selected = [
        int(row["id"])
        for row in repairs
        if latest_adj.get(str(row["fund_code"]), "") >= str(row.get("gap_end") or "")
    ]
    return selected, latest_adj


def _create_backup_table(connection, source: str, backup: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE TABLE `{backup}` LIKE `{source}`")
    connection.commit()


def converge_repairs(connection, stamp: str, dry_run: bool) -> dict:
    """备份并关闭经过事实验证的陈旧 adj failed。"""
    selected, latest_adj = load_repair_selection(connection)
    result = {
        "cutoff": REPAIR_CUTOFF,
        "selection": "task_kind=adj,status=failed,gap_end<=cutoff,latest_adj>=gap_end",
        "adj_fact_funds": len(latest_adj),
        "verified_candidates": len(selected),
    }
    if dry_run:
        return result
    backup_table = (
        f"nav_repair_queue_backup_{stamp.replace('+', 'p').replace('-', 'm')}"
    )
    _create_backup_table(connection, "nav_repair_queue", backup_table)
    try:
        with connection.cursor() as cursor:
            for batch in chunks(selected):
                placeholders = ",".join(["%s"] * len(batch))
                cursor.execute(
                    f"INSERT INTO `{backup_table}` SELECT * FROM nav_repair_queue "
                    f"WHERE id IN ({placeholders})",
                    batch,
                )
            cursor.execute(f"SELECT COUNT(*) n FROM `{backup_table}`")
            backed_up = int(cursor.fetchone()["n"])
            if backed_up != len(selected):
                raise RuntimeError("repair 备份行数与选择数不一致")
            closed = 0
            for batch in chunks(selected):
                placeholders = ",".join(["%s"] * len(batch))
                cursor.execute(
                    "UPDATE nav_repair_queue SET status='done',last_error=NULL,"
                    f"next_retry_at=NULL,updated_at=NOW() WHERE id IN ({placeholders}) "
                    "AND status='failed'",
                    batch,
                )
                closed += int(cursor.rowcount)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    if closed != len(selected):
        raise RuntimeError("repair 关闭数与已验证选择数不一致")
    result.update(
        {
            "backup_table": backup_table,
            "backed_up": backed_up,
            "closed": closed,
            "rollback": (
                f"UPDATE nav_repair_queue q JOIN `{backup_table}` b ON q.id=b.id "
                "SET q.status=b.status,q.attempts=b.attempts,q.next_retry_at=b.next_retry_at,"
                "q.last_error=b.last_error,q.updated_at=b.updated_at"
            ),
        }
    )
    return result


def load_fact_and_state_watermarks(
    connection,
) -> tuple[dict[str, str], dict[str, str | None]]:
    """在 Python 中比较事实/状态，规避两表历史 collation 不一致。"""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT fund_code,MAX(trade_date) latest FROM fund_nav GROUP BY fund_code"
        )
        facts = {str(row["fund_code"]): str(row["latest"]) for row in cursor.fetchall()}
        cursor.execute(
            "SELECT fund_code,watermark_date FROM fund_sync_state WHERE task_kind='nav'"
        )
        states = {
            str(row["fund_code"]): str(row["watermark_date"])
            if row["watermark_date"]
            else None
            for row in cursor.fetchall()
        }
    return facts, states


def converge_watermarks(connection, stamp: str, dry_run: bool) -> dict:
    """以事实表实际最新日重建所有滞后/缺失 nav 水位。"""
    facts, states = load_fact_and_state_watermarks(connection)
    lagging = {
        code: day
        for code, day in facts.items()
        if not states.get(code) or states[code] < day
    }
    missing_state = sum(code not in states for code in lagging)
    result = {
        "fact_funds": len(facts),
        "state_funds_before": len(states),
        "lagging_before": len(lagging),
        "missing_state_before": missing_state,
    }
    if dry_run:
        return result
    backup_table = f"fund_sync_state_backup_{stamp.replace('+', 'p').replace('-', 'm')}"
    _create_backup_table(connection, "fund_sync_state", backup_table)
    existing_codes = [code for code in lagging if code in states]
    try:
        with connection.cursor() as cursor:
            for batch in chunks(existing_codes):
                placeholders = ",".join(["%s"] * len(batch))
                cursor.execute(
                    f"INSERT INTO `{backup_table}` SELECT * FROM fund_sync_state "
                    f"WHERE task_kind='nav' AND fund_code IN ({placeholders})",
                    batch,
                )
            sql = (
                "INSERT INTO fund_sync_state "
                "(fund_code,task_kind,watermark_date,status,attempts,last_error) "
                "VALUES (%s,'nav',%s,'success',0,NULL) "
                "ON DUPLICATE KEY UPDATE watermark_date=VALUES(watermark_date),"
                "status='success',attempts=0,last_error=NULL"
            )
            for batch in chunks(list(lagging.items())):
                cursor.executemany(sql, batch)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    facts_after, states_after = load_fact_and_state_watermarks(connection)
    lagging_after = {
        code: day
        for code, day in facts_after.items()
        if not states_after.get(code) or states_after[code] < day
    }
    if lagging_after:
        raise RuntimeError(f"水位重建后仍滞后 {len(lagging_after)} 条")
    result.update(
        {
            "rebuilt": len(lagging),
            "backup_table": backup_table,
            "backed_up_existing": len(existing_codes),
            "state_funds_after": len(states_after),
            "lagging_after": 0,
            "rollback": (
                f"先删除本批新增的 {missing_state} 条 nav state，再以 `{backup_table}` "
                "按主键恢复原 watermark/status/error"
            ),
        }
    )
    return result


def run(args: argparse.Namespace) -> dict:
    """执行或只读预演全部收敛步骤。"""
    stamp = timestamp()
    result = {
        "started_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "dry_run": args.dry_run,
        "blacklist": converge_blacklist(
            args.blacklist.resolve(), args.backup_dir.resolve(), stamp, args.dry_run
        ),
    }
    connection = connect_mysql()
    try:
        result["repair"] = converge_repairs(connection, stamp, args.dry_run)
        result["watermarks"] = converge_watermarks(connection, stamp, args.dry_run)
    finally:
        connection.close()
    result["finished_at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    atomic_json(args.evidence.resolve(), result)
    return result


def build_parser() -> argparse.ArgumentParser:
    """构建受限 CLI。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--blacklist", type=Path, default=BACKEND_DIR / "nav_blacklist.db"
    )
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    """CLI 入口。"""
    args = build_parser().parse_args()
    result = run(args)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
