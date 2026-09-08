#!/usr/bin/env python3
"""iFund 日常净值同步调度入口。

轮次：20:00 至 23:45 ``incremental``、23:50 ``finalize``、次日 08:00 ``reconcile``。
Howbuy 不在主链；AkShare 仅在 ``--enable-akshare`` 或环境开关显式启用。
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)

from app.fund_nav import alerts as nav_alerts  # pylint: disable=wrong-import-position
from app.fund_nav import daily_sync  # pylint: disable=wrong-import-position

LOG_DIR = BACKEND_DIR / "logs"
DEFAULT_PLAN_PATH = LOG_DIR / "daily_nav_poll_state.json"
DEFAULT_EVIDENCE_DIR = LOG_DIR / "nav_sync_evidence"
INTERNAL_LOCK_PATH = Path("/run/lock/ifund-daily-nav-internal.lock")
NIGHT_START = dt.time(20, 0)
NIGHT_FINAL = dt.time(23, 50)
NIGHT_END = dt.time(23, 59, 59)
NIGHT_POLL_INTERVAL_MINUTES = 5
logger = logging.getLogger("daily_nav_sync")


def configure_logging(run_date: dt.date) -> None:
    """同时写入当日日志和标准输出。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(
        LOG_DIR / f"daily_nav_{run_date:%Y%m%d}.log", encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    daily_sync.logger.handlers = logger.handlers
    daily_sync.logger.setLevel(logging.INFO)
    daily_sync.logger.propagate = False


@contextmanager
def internal_lock() -> Iterator[bool]:
    """cron flock 之外再阻止手工并发。"""
    INTERNAL_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = INTERNAL_LOCK_PATH.open("a+", encoding="utf-8")
    acquired = False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        if acquired:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def resolve_round(raw_round: str, now: dt.datetime | None = None) -> str | None:
    """解析轮次；保留旧 first/second/morning/noon 参数兼容。"""
    current = now or dt.datetime.now().astimezone()
    current_time = current.time().replace(tzinfo=None)
    aliases = {
        "first": "incremental",
        "second": "finalize",
        "morning": "reconcile",
        "noon": "reconcile",
    }
    if raw_round != "auto":
        if raw_round == "night":
            return "finalize" if current_time >= NIGHT_FINAL else "incremental"
        return aliases.get(raw_round, raw_round)
    if NIGHT_START <= current_time < NIGHT_FINAL:
        return "incremental"
    if NIGHT_FINAL <= current_time <= NIGHT_END:
        return "finalize"
    if 7 <= current.hour < 14:
        return "reconcile"
    return None


def night_poll_slots() -> tuple[dt.time, ...]:
    """返回 cron 应覆盖的 47 个五分钟时点，供边界测试和部署校验。"""
    anchor = dt.datetime.combine(dt.date(2000, 1, 1), NIGHT_START)
    final = dt.datetime.combine(anchor.date(), NIGHT_FINAL)
    slots = []
    while anchor <= final:
        slots.append(anchor.time())
        anchor += dt.timedelta(minutes=NIGHT_POLL_INTERVAL_MINUTES)
    return tuple(slots)


def _env_enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _evidence_path(directory: Path, round_name: str, now: dt.datetime) -> Path:
    stamp = now.astimezone().strftime("%Y%m%dT%H%M%S%z")
    return directory / f"daily-nav-{round_name}-{stamp}.json"


def build_parser() -> argparse.ArgumentParser:
    """构建调度 CLI。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--round",
        choices=(
            "auto",
            "incremental",
            "finalize",
            "reconcile",
            "night",
            "first",
            "second",
            "morning",
            "noon",
        ),
        default="auto",
    )
    parser.add_argument("--plan-path", type=Path, default=DEFAULT_PLAN_PATH)
    parser.add_argument("--evidence-dir", type=Path, default=DEFAULT_EVIDENCE_DIR)
    parser.add_argument("--alert-dir", type=Path, default=nav_alerts.DEFAULT_ALERT_DIR)
    parser.add_argument("--eastmoney-concurrency", type=int, default=4)
    parser.add_argument("--tushare-budget", type=int, default=50)
    parser.add_argument("--enable-akshare", action="store_true")
    parser.add_argument("--today", help="仅测试/受控补跑使用的 YYYY-MM-DD")
    return parser


def main(argv: list[str] | None = None) -> int:
    """运行指定轮次并保存 JSON 证据。"""
    args = build_parser().parse_args(argv)
    current = dt.datetime.now().astimezone()
    configure_logging(current.date())
    round_name = resolve_round(args.round, current)
    if round_name is None:
        logger.info("当前时间不在净值同步调度窗口，退出")
        return 0
    try:
        today = (
            dt.date.fromisoformat(args.today).isoformat()
            if args.today
            else current.date().isoformat()
        )
    except ValueError as exc:
        raise SystemExit("--today 必须是 YYYY-MM-DD") from exc
    akshare_enabled = args.enable_akshare or _env_enabled(
        "IFUND_ENABLE_AKSHARE_FALLBACK"
    )
    evidence_path = _evidence_path(args.evidence_dir.resolve(), round_name, current)

    with internal_lock() as acquired:
        if not acquired:
            logger.warning("已有 daily_nav_sync 实例运行，本轮退出")
            return 75
        connection = None
        try:
            connection = daily_sync.connect_mysql()
            if round_name in {
                "incremental",
                "finalize",
            } and not daily_sync.is_trade_date(connection, today):
                logger.info("非交易日跳过（%s 节假日/休市）", today)
                return 0
            if round_name != "incremental":
                logger.info(
                    "[%s] 启动：Tushare 日期窗口→东财补漏→AkShare(%s)，Howbuy=禁用，fetch_tasks=停写",
                    round_name,
                    "显式启用" if akshare_enabled else "默认关闭",
                )
            common = {
                "eastmoney_concurrency": max(1, min(16, args.eastmoney_concurrency)),
                "akshare_enabled": akshare_enabled,
            }
            if round_name == "incremental":
                report = daily_sync.run_incremental(
                    connection,
                    today=today,
                    plan_path=args.plan_path.resolve(),
                    budget_maximum=max(1, args.tushare_budget),
                )
            elif round_name == "finalize":
                report = daily_sync.run_finalize(
                    connection,
                    today=today,
                    plan_path=args.plan_path.resolve(),
                    budget_maximum=max(1, args.tushare_budget),
                    **common,
                )
            else:
                report = daily_sync.run_reconcile(
                    connection,
                    today=today,
                    budget_maximum=max(1, args.tushare_budget),
                    **common,
                )
            if round_name == "incremental":
                if report["noop_streak"]:
                    logger.info(
                        "[incremental] 目标日=%s 披露行=%d 新增=0 连续无变化=%d Tushare调用=%d",
                        report["target_date"],
                        report["tushare_mapped"],
                        report["noop_streak"],
                        report["source_health"]["tushare_calls"],
                    )
                else:
                    logger.info(
                        "[incremental] 目标日=%s 披露行=%d 新增=%d 目标切换=%s Tushare调用=%d",
                        report["target_date"],
                        report["tushare_mapped"],
                        report["tushare_inserted"],
                        report["target_changed"],
                        report["source_health"]["tushare_calls"],
                    )
                return 0
            monitoring = nav_alerts.evaluate(
                connection, report, observed_on=current.date()
            )
            monitoring["files"] = nav_alerts.emit(
                monitoring,
                alert_dir=args.alert_dir,
                alert_logger=logger,
            )
            report["monitoring"] = monitoring
            daily_sync.atomic_json(evidence_path, report)
            gate = report["integrity_gate"]
            logger.info(
                "[%s] 完成：目标日=%s 行数=%s 覆盖率=%.2f%% Tushare调用=%d 东财成功=%d 证据=%s",
                round_name,
                report["target_date"],
                gate["target_rows"],
                gate["coverage"] * 100,
                report["source_health"]["tushare_calls"],
                report["source_health"]["eastmoney_success"],
                evidence_path,
            )
            return 1 if monitoring["alerts"] else 0
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.exception("[%s] 同步失败", round_name)
            failure = {
                "round": round_name,
                "started_at": current.isoformat(timespec="seconds"),
                "failed_at": daily_sync.now_iso(),
                "error_type": type(exc).__name__,
                "error": str(exc)[:1000],
                "howbuy": "removed_from_daily_chain",
                "fetch_tasks_audit": "disabled",
            }
            if round_name != "incremental":
                daily_sync.atomic_json(evidence_path, failure)
                logger.error("失败证据=%s", evidence_path)
            return 2
        finally:
            if connection is not None:
                connection.close()


if __name__ == "__main__":
    EXIT_CODE = main()
    if EXIT_CODE:
        print(json.dumps({"exit_code": EXIT_CODE}, ensure_ascii=False))
    raise SystemExit(EXIT_CODE)
