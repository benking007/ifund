"""净值完整性最小告警：结构化文件、单行 ERROR 与退出码判据。"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Any

from app.fund_nav.fetch import no_nav_blacklist

DEFAULT_ALERT_DIR = Path(__file__).resolve().parents[2] / "logs" / "nav_alerts"
DEFAULT_COVERAGE_THRESHOLD = 0.8
DEFAULT_NO_UPDATE_TRADE_DAYS = 2
DEFAULT_BLACKLIST_DAILY_THRESHOLD = 50
DEFAULT_REPAIR_MAX_AGE_HOURS = 24


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def thresholds() -> dict[str, float | int]:
    """读取可调阈值；默认值即本批治理基线。"""
    return {
        "coverage_ratio": _env_float(
            "IFUND_NAV_ALERT_COVERAGE_RATIO", DEFAULT_COVERAGE_THRESHOLD
        ),
        "no_update_trade_days": _env_int(
            "IFUND_NAV_ALERT_NO_UPDATE_TRADE_DAYS", DEFAULT_NO_UPDATE_TRADE_DAYS
        ),
        "blacklist_daily_new": _env_int(
            "IFUND_NAV_ALERT_BLACKLIST_DAILY_NEW", DEFAULT_BLACKLIST_DAILY_THRESHOLD
        ),
        "repair_max_age_hours": _env_int(
            "IFUND_NAV_ALERT_REPAIR_MAX_AGE_HOURS", DEFAULT_REPAIR_MAX_AGE_HOURS
        ),
        "watermark_fact_mismatch": 0,
    }


def _fetchone(cursor, sql: str, args: tuple[Any, ...] = ()) -> dict:
    cursor.execute(sql, args)
    row = cursor.fetchone() or {}
    return dict(row)


def _watermark_fact_mismatch(cursor, *, batch_size: int = 5000) -> int:
    """分批用 ``fund_nav(fund_code,trade_date)`` 唯一键核验水位。"""
    cursor.execute(
        "SELECT fund_code,watermark_date FROM fund_sync_state "
        "WHERE task_kind='nav' AND watermark_date IS NOT NULL"
    )
    expected = [
        (str(row["fund_code"]), str(row["watermark_date"])) for row in cursor.fetchall()
    ]
    matched: set[tuple[str, str]] = set()
    for start in range(0, len(expected), batch_size):
        batch = expected[start : start + batch_size]
        placeholders = ",".join(["(%s,%s)"] * len(batch))
        params = [value for key in batch for value in key]
        cursor.execute(
            "SELECT fund_code,trade_date FROM fund_nav WHERE "
            f"(fund_code,trade_date) IN ({placeholders})",
            params,
        )
        matched.update(
            (str(row["fund_code"]), str(row["trade_date"])) for row in cursor.fetchall()
        )
    return len(expected) - len(matched)


def collect_observations(
    connection,
    target_date: str,
    *,
    observed_on: dt.date | None = None,
) -> dict[str, Any]:
    """读取告警所需最小事实；所有 MySQL 查询只读且走业务键。"""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT trade_date FROM trade_dates WHERE trade_date<=%s "
            "ORDER BY trade_date DESC LIMIT 260",
            (target_date,),
        )
        calendar = [str(row["trade_date"]) for row in cursor.fetchall()]
        latest_fact = _fetchone(
            cursor, "SELECT MAX(trade_date) AS latest FROM fund_nav"
        ).get("latest")
        repair = _fetchone(
            cursor,
            "SELECT COUNT(*) AS open_count,MIN(created_at) AS oldest_created_at,"
            "COALESCE(MAX(TIMESTAMPDIFF(HOUR,created_at,NOW())),0) AS oldest_age_hours "
            "FROM nav_repair_queue WHERE status IN ('pending','failed','running')",
        )
        mismatch_count = _watermark_fact_mismatch(cursor)

    latest_text = str(latest_fact) if latest_fact else None
    missing_trade_days = sum(
        1 for trade_date in calendar if latest_text is None or trade_date > latest_text
    )
    run_date = observed_on or dt.datetime.now().astimezone().date()
    blacklist_new = 0
    for row in no_nav_blacklist.list_records():
        try:
            detected = dt.datetime.fromisoformat(str(row.get("detected_at") or ""))
        except ValueError:
            continue
        if detected.date() == run_date:
            blacklist_new += 1
    return {
        "target_date": target_date,
        "latest_fact_date": latest_text,
        "consecutive_trade_days_without_update": missing_trade_days,
        "blacklist_observed_on": run_date.isoformat(),
        "blacklist_daily_new": blacklist_new,
        "repair_open_count": int(repair.get("open_count") or 0),
        "repair_oldest_created_at": repair.get("oldest_created_at"),
        "repair_oldest_age_hours": int(repair.get("oldest_age_hours") or 0),
        "watermark_fact_mismatch": mismatch_count,
    }


def _alert(code: str, message: str, value: Any, threshold: Any) -> dict[str, Any]:
    return {
        "code": code,
        "severity": "error",
        "message": message,
        "value": value,
        "threshold": threshold,
    }


def build_payload(
    report: dict,
    observations: dict[str, Any],
    *,
    configured_thresholds: dict[str, float | int] | None = None,
) -> dict[str, Any]:
    """把运行报告与事实转换为稳定、可测试的告警判据。"""
    limits = configured_thresholds or thresholds()
    gate = report.get("integrity_gate") or {}
    coverage = float(gate.get("coverage") or 0.0)
    baseline = float(gate.get("baseline_median") or 0.0)
    alerts: list[dict[str, Any]] = []
    if baseline and coverage < float(limits["coverage_ratio"]):
        alerts.append(
            _alert(
                "nav_coverage_below_baseline",
                "目标日净值覆盖率低于近十交易日基线的 80%",
                coverage,
                limits["coverage_ratio"],
            )
        )
    no_update_days = int(observations.get("consecutive_trade_days_without_update") or 0)
    if no_update_days >= int(limits["no_update_trade_days"]):
        alerts.append(
            _alert(
                "nav_no_update_two_trade_days",
                "净值事实表已连续至少两个交易日无更新",
                no_update_days,
                limits["no_update_trade_days"],
            )
        )
    blacklist_new = int(observations.get("blacklist_daily_new") or 0)
    if blacklist_new > int(limits["blacklist_daily_new"]):
        alerts.append(
            _alert(
                "nav_blacklist_daily_spike",
                "无净值黑名单单日新增超过阈值",
                blacklist_new,
                limits["blacklist_daily_new"],
            )
        )
    repair_age = int(observations.get("repair_oldest_age_hours") or 0)
    if observations.get("repair_open_count") and repair_age > int(
        limits["repair_max_age_hours"]
    ):
        alerts.append(
            _alert(
                "nav_repair_queue_overage",
                "净值修复队列最老未闭环任务超过时限",
                repair_age,
                limits["repair_max_age_hours"],
            )
        )
    mismatch = int(observations.get("watermark_fact_mismatch") or 0)
    if mismatch > int(limits["watermark_fact_mismatch"]):
        alerts.append(
            _alert(
                "nav_watermark_fact_mismatch",
                "净值水位指向的事实行不存在",
                mismatch,
                limits["watermark_fact_mismatch"],
            )
        )
    return {
        "schema_version": 1,
        "component": "ifund.daily_nav_sync",
        "status": "alert" if alerts else "ok",
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "round": report.get("round"),
        "target_date": report.get("target_date"),
        "thresholds": limits,
        "observations": observations,
        "alerts": alerts,
    }


def evaluate(connection, report: dict, *, observed_on: dt.date | None = None) -> dict:
    """采集事实并执行全部告警判据。"""
    observations = collect_observations(
        connection, str(report["target_date"]), observed_on=observed_on
    )
    return build_payload(report, observations)


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def emit(
    payload: dict,
    *,
    alert_dir: Path = DEFAULT_ALERT_DIR,
    alert_logger: logging.Logger | None = None,
) -> dict[str, str | None]:
    """落统一 JSON，并输出唯一 NAV_ALERT ERROR 行。

    未来接 IM/邮件时，在本函数成功落盘后调用外部 dispatcher/webhook 即可；
    判据与 daily_sync 调度入口无需再改。
    """
    destination = alert_dir.resolve()
    latest_path = destination / "latest.json"
    _atomic_json(latest_path, payload)
    event_path: Path | None = None
    log = alert_logger or logging.getLogger(__name__)
    if payload.get("alerts"):
        stamp = dt.datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
        event_path = destination / f"nav-alert-{stamp}.json"
        _atomic_json(event_path, payload)
        summary = {
            "status": payload.get("status"),
            "round": payload.get("round"),
            "target_date": payload.get("target_date"),
            "codes": [item.get("code") for item in payload.get("alerts", [])],
            "event_file": str(event_path),
        }
        log.error(
            "NAV_ALERT %s", json.dumps(summary, ensure_ascii=False, sort_keys=True)
        )
    else:
        log.info("NAV_ALERT_OK target_date=%s", payload.get("target_date"))
    return {
        "latest": str(latest_path),
        "event": str(event_path) if event_path else None,
    }
