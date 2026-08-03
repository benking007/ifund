#!/usr/bin/env python3
"""按披露节奏增量同步基金净值。

这个脚本只负责调度基金集合和并发度，单只基金的幂等、增量和失败回退全部
交给 ``app.fund_nav.fetch.worker._process_one``。因此同一轮被重复执行、进程
中途退出或下一轮重新启动，都不会破坏断点续跑语义。

默认分组（实际 ``funds.type`` 字段）：

* P1：货币、债券、固收指数；
* P2：普通股票/混合、境内股票指数、REITs，以及名称标注港股/恒生的 QDII；
* P3：QDII 海外/美股/全球、商品、FOF，以及未识别的新类型。

用法示例：

    venv/bin/python3.12 scripts/daily_nav_sync.py
    venv/bin/python3.12 scripts/daily_nav_sync.py --round morning
    venv/bin/python3.12 scripts/daily_nav_sync.py --round noon --once

``--type-priority`` 支持 ``P1,P2,P3``（默认分组）、分号分隔的自定义分组
（如 ``P1=货币型*,债券型*;P2=股票型,混合型*;P3=*``），也支持简单的
逗号类型清单；简单清单中的每个类型按给定顺序作为一个批次。
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import fnmatch
import logging
import os
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - backend requirements normally provide it
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv(BACKEND_DIR / ".env")

from app import db as database  # noqa: E402  pylint: disable=wrong-import-position
from app.trade_calendar.crud import calendar_crud  # noqa: E402  pylint: disable=wrong-import-position


SCRIPT_SLUG = "daily_nav_sync"
LOG_DIR = BACKEND_DIR / "logs"
PID_FILE = LOG_DIR / f"{SCRIPT_SLUG}.pid"
NIGHT_END_HOUR = 23
NIGHT_SWITCH_HOUR = 22
NIGHT_SWITCH_MINUTE = 30
ROUND_INTERVAL_SECONDS = 5 * 60
MAX_BACKOFF_SECONDS = 10 * 60
DEFAULT_CONCURRENCY = 8
DEFAULT_FAIL_THRESHOLD = 50
DEFAULT_NETWORK_ERROR_THRESHOLD = 10

NETWORK_ERROR_MARKERS = (
    "timeout", "timed out", "connection", "network", "proxy", "dns",
    "remote", "http", "ssl", "reset", "refused", "连接", "网络", "超时",
)


@dataclass(frozen=True)
class TypeGroup:
    """一个按优先级执行的基金类型批次。"""

    label: str
    selectors: tuple[str, ...]
    include_unmatched: bool = False


@dataclass
class ProcessStats:
    """单个类型批次的执行结果。"""

    total: int = 0
    success: int = 0
    skip: int = 0
    fail: int = 0
    network_fail: int = 0

    def add(self, status: str, error: str = "") -> None:
        if status == "skip":
            self.skip += 1
        elif status == "success":
            self.success += 1
        else:
            self.fail += 1
            if _looks_like_network_error(error):
                self.network_fail += 1


@dataclass
class RoundStats:
    """一轮的聚合执行结果。"""

    total: int = 0
    success: int = 0
    skip: int = 0
    fail: int = 0
    network_fail: int = 0

    def add(self, stats: ProcessStats) -> None:
        self.total += stats.total
        self.success += stats.success
        self.skip += stats.skip
        self.fail += stats.fail
        self.network_fail += stats.network_fail


DEFAULT_GROUPS: tuple[TypeGroup, ...] = (
    TypeGroup(
        "P1",
        (
            "货币型*",
            "债券型*",
            "指数型-固收",
        ),
    ),
    TypeGroup(
        "P2",
        (
            "股票型",
            "混合型*",
            "指数型-股票",
            "Reits",
            "QDII-REITs",
            "QDII-港股*",
            "@REIT",
            "@QDII_HK",
        ),
    ),
    TypeGroup(
        "P3",
        (
            "QDII-商品*",
            "商品*",
            "FOF*",
            "QDII-FOF*",
            "QDII-美股*",
            "QDII-全球*",
            "QDII-海外*",
            "@QDII_OVERSEAS",
        ),
        include_unmatched=True,
    ),
)

MORNING_GROUP = TypeGroup(
    "MORNING",
    (
        "QDII-商品*",
        "商品*",
        "FOF*",
        "QDII-FOF*",
    ),
)

DEFAULT_GROUP_BY_LABEL = {group.label: group for group in DEFAULT_GROUPS}


_PROCESS_ONE = None
logger = logging.getLogger(SCRIPT_SLUG)


def configure_logging(run_date: dt.date) -> None:
    """把当天日志同时写入 daily_nav_YYYYMMDD.log 和标准输出。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"daily_nav_{run_date:%Y%m%d}.log"
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)


@contextmanager
def single_instance_lock() -> Iterator[bool]:
    """用持锁文件防止同一 slug 的夜间循环被重复启动。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    lock_handle = PID_FILE.open("a+", encoding="utf-8")
    acquired = False
    try:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            logger.warning("[%s] 已有实例运行（锁文件：%s），本次退出", SCRIPT_SLUG, PID_FILE)
            yield False
            return

        lock_handle.seek(0)
        lock_handle.truncate()
        lock_handle.write(str(os.getpid()))
        lock_handle.flush()
        yield True
    finally:
        if acquired:
            lock_handle.seek(0)
            lock_handle.truncate()
            lock_handle.flush()
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()


def _normalise(value: object) -> str:
    return str(value or "").strip()


def _is_qdii_row(row: dict) -> bool:
    type_name = _normalise(row.get("type")).upper()
    fund_name = _normalise(row.get("name")).upper()
    return "QDII" in type_name or "QDII" in fund_name


def _is_reit_row(row: dict) -> bool:
    value = f"{_normalise(row.get('type'))} {_normalise(row.get('name'))}".upper()
    return "REIT" in value


def _is_hk_qdii_row(row: dict) -> bool:
    type_name = _normalise(row.get("type"))
    if not (_is_qdii_row(row) or type_name == "指数型-海外股票"):
        return False
    value = f"{_normalise(row.get('type'))} {_normalise(row.get('name'))}"
    return any(marker in value for marker in ("港股", "香港", "恒生", "港币"))


def _is_overseas_qdii_row(row: dict) -> bool:
    type_name = _normalise(row.get("type"))
    if not (_is_qdii_row(row) or type_name == "指数型-海外股票"):
        return False
    if type_name.startswith(("QDII-商品", "QDII-FOF")):
        return False
    if _is_reit_row(row) or _is_hk_qdii_row(row):
        return False
    return True


def _matches_selector(row: dict, selector: str) -> bool:
    """匹配实际类型或少量默认分组别名。"""
    selector = selector.strip()
    if not selector:
        return not _normalise(row.get("type"))
    if selector == "@REIT":
        return _is_reit_row(row)
    if selector == "@QDII_HK":
        return _is_hk_qdii_row(row)
    if selector == "@QDII_OVERSEAS":
        return _is_overseas_qdii_row(row)

    type_name = _normalise(row.get("type"))
    if fnmatch.fnmatchcase(type_name, selector):
        return True
    # 对不带通配符的自定义类别，允许「债券型」匹配「债券型-长债」这类实际值。
    if not any(char in selector for char in "*?["):
        return type_name.startswith(f"{selector}-")
    return False


def _split_selectors(value: str) -> list[str]:
    return [item.strip() for item in value.replace("，", ",").split(",") if item.strip()]


def parse_type_priority(raw: str) -> tuple[TypeGroup, ...]:
    """解析 ``--type-priority``，未提供时返回默认 P1/P2/P3。"""
    text = (raw or "").strip()
    if not text or text.lower() in {"default", "默认"}:
        return DEFAULT_GROUPS

    text = text.replace("，", ",")
    named_parts = _split_selectors(text)
    if named_parts and all(part.upper() in DEFAULT_GROUP_BY_LABEL for part in named_parts):
        return tuple(DEFAULT_GROUP_BY_LABEL[part.upper()] for part in named_parts)

    # 分号、竖线、斜杠表示多个优先级批次；每段可写成 label=type1,type2。
    segments = [segment.strip() for segment in re.split(r"[;|/]", text) if segment.strip()]
    if len(segments) > 1:
        groups: list[TypeGroup] = []
        for index, segment in enumerate(segments, 1):
            label = f"T{index}"
            selector_text = segment
            for separator in ("=", ":"):
                if separator in segment:
                    label, selector_text = segment.split(separator, 1)
                    label = label.strip() or f"T{index}"
                    break
            groups.append(TypeGroup(label, tuple(_split_selectors(selector_text))))
        return _with_last_group_fallback(groups)

    # 简单逗号清单：每个 selector 是一个按顺序处理的批次。
    groups = [TypeGroup(f"T{index}", (selector,))
              for index, selector in enumerate(_split_selectors(text), 1)]
    return _with_last_group_fallback(groups)


def _with_last_group_fallback(groups: list[TypeGroup]) -> tuple[TypeGroup, ...]:
    # 自定义清单只处理清单中声明的类型；需要全量兜底时显式传 ``*``，或使用默认
    # P1/P2/P3（默认 P3 自带 include_unmatched=True）。
    return tuple(groups) if groups else DEFAULT_GROUPS


def resolve_round(raw_round: str, now: dt.datetime | None = None) -> str | None:
    """将 auto 映射为当前调度轮次；非调度时间返回 None。"""
    if raw_round != "auto":
        return raw_round
    current = now or dt.datetime.now()
    if 20 <= current.hour <= 23:
        return "night"
    if 7 <= current.hour < 11:
        return "morning"
    if 11 <= current.hour < 15:
        return "noon"
    return None


def is_trading_day(day: dt.date) -> bool:
    """只允许交易日运行；节假日由本地交易日历决定。"""
    day_text = day.isoformat()
    if day.weekday() >= 5:
        logger.info("%s 是周末，跳过净值同步", day_text)
        return False
    try:
        dates = calendar_crud.list_dates(str(day.year))
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.exception("读取交易日历失败，出于安全考虑跳过 %s：%s", day_text, exc)
        return False
    if day_text not in set(dates):
        logger.info("%s 不在交易日历中，跳过净值同步", day_text)
        return False
    return True


def load_fund_rows() -> list[dict]:
    """读取基金 code/type/name；不读取净值，跳过判定留给 ``_process_one``。"""
    rows = database.select(
        "funds",
        [
            ("select", "code,type,name"),
            ("order", "type.asc,code.asc"),
            ("limit", "1000000"),
        ],
    )
    unique_rows: list[dict] = []
    seen_codes: set[str] = set()
    for row in rows:
        code = _normalise(row.get("code"))
        if code and code not in seen_codes:
            seen_codes.add(code)
            unique_rows.append({
                "code": code,
                "type": _normalise(row.get("type")),
                "name": _normalise(row.get("name")),
            })
    return unique_rows


def group_funds(
    rows: list[dict], groups: tuple[TypeGroup, ...],
) -> list[tuple[TypeGroup, dict[str, list[str]]]]:
    """按优先级分组，并保证每个基金 code 只出现在一个批次。"""
    remaining = list(rows)
    grouped: list[tuple[TypeGroup, dict[str, list[str]]]] = []
    for group in groups:
        matched: list[dict] = []
        still_remaining: list[dict] = []
        for row in remaining:
            if any(_matches_selector(row, selector) for selector in group.selectors):
                matched.append(row)
            else:
                still_remaining.append(row)
        if group.include_unmatched:
            matched.extend(still_remaining)
            still_remaining = []

        by_type: dict[str, list[str]] = defaultdict(list)
        for row in matched:
            by_type[_normalise(row.get("type")) or "(空类型)"].append(row["code"])
        grouped.append((group, dict(by_type)))
        remaining = still_remaining

    if remaining:
        # 仅在自定义分组最后一组没有声明兜底时触发；默认 P3 不会走这里。
        fallback = TypeGroup("UNMATCHED", (), include_unmatched=True)
        by_type: dict[str, list[str]] = defaultdict(list)
        for row in remaining:
            by_type[_normalise(row.get("type")) or "(空类型)"].append(row["code"])
        grouped.append((fallback, dict(by_type)))
    return grouped


def _worker_concurrency() -> int:
    raw = os.getenv("IFUND_WORKER_CONCURRENCY", str(DEFAULT_CONCURRENCY))
    try:
        return min(64, max(1, int(raw)))
    except ValueError:
        logger.warning("IFUND_WORKER_CONCURRENCY=%r 无效，回退为 %d", raw, DEFAULT_CONCURRENCY)
        return DEFAULT_CONCURRENCY


def _threshold(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _looks_like_network_error(error: str) -> bool:
    lowered = error.lower()
    return any(marker in lowered for marker in NETWORK_ERROR_MARKERS)


def _process_one_func():
    """延迟导入重型净值 worker，令重复实例能在导入 AkShare 前退出。"""
    global _PROCESS_ONE
    if _PROCESS_ONE is None:
        from app.fund_nav.fetch.worker import _process_one  # pylint: disable=import-outside-toplevel
        _PROCESS_ONE = _process_one
    return _PROCESS_ONE


def _safe_process(code: str) -> tuple[str, str]:
    try:
        status = _process_one_func()(code) or "success"
        return str(status), ""
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.exception("基金 %s 处理异常：%s", code, exc)
        return "fail", str(exc)


def process_codes(codes: list[str], concurrency: int) -> ProcessStats:
    """以固定并发执行一个实际类型批次，只调用 ``_process_one``。"""
    stats = ProcessStats(total=len(codes))
    if not codes:
        return stats
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix=SCRIPT_SLUG) as executor:
        futures = [executor.submit(_safe_process, code) for code in codes]
        for future in as_completed(futures):
            status, error = future.result()
            stats.add(status, error)
    return stats


def _format_stats(round_name: str, type_name: str, stats: ProcessStats, elapsed: float) -> str:
    return (
        f"[{round_name}] {type_name} 待拉 {stats.total} 成功 {stats.success} "
        f"跳过 {stats.skip} 失败 {stats.fail} 耗时 {elapsed:.1f} 秒"
    )


def active_groups(
    groups: tuple[TypeGroup, ...], round_name: str, now: dt.datetime,
) -> tuple[TypeGroup, ...]:
    if round_name == "morning":
        return (MORNING_GROUP,)
    if round_name == "noon":
        return groups
    if len(groups) <= 1:
        return groups
    if (now.hour, now.minute) >= (NIGHT_SWITCH_HOUR, NIGHT_SWITCH_MINUTE):
        return (groups[-1],)
    return groups[:-1]


def run_round(
    round_name: str,
    groups: tuple[TypeGroup, ...],
    now: dt.datetime | None = None,
) -> RoundStats:
    """执行一轮，按优先级逐类型处理并输出统计行。"""
    current = now or dt.datetime.now()
    routing_groups = (MORNING_GROUP,) if round_name == "morning" else groups
    rows = load_fund_rows()
    grouped = group_funds(rows, routing_groups)
    selected_groups = active_groups(routing_groups, round_name, current)
    selected = {group.label for group in selected_groups}
    concurrency = _worker_concurrency()
    round_stats = RoundStats()

    logger.info(
        "[%s] 开始：基金 %d 只，并发 %d，活动分组 %s",
        round_name,
        len(rows),
        concurrency,
        ",".join(group.label for group in selected_groups) or "无",
    )
    for group, by_type in grouped:
        if group.label not in selected:
            continue
        for type_name in sorted(by_type):
            codes = by_type[type_name]
            started = time.monotonic()
            stats = process_codes(codes, concurrency)
            elapsed = time.monotonic() - started
            round_stats.add(stats)
            logger.info(_format_stats(round_name, type_name, stats, elapsed))

    logger.info(
        "[%s] 本轮合计：待拉 %d 成功 %d 跳过 %d 失败 %d 网络异常 %d",
        round_name,
        round_stats.total,
        round_stats.success,
        round_stats.skip,
        round_stats.fail,
        round_stats.network_fail,
    )
    return round_stats


def _night_deadline(now: dt.datetime) -> dt.datetime:
    return now.replace(hour=NIGHT_END_HOUR, minute=0, second=0, microsecond=0)


def run_night(groups: tuple[TypeGroup, ...], once: bool) -> None:
    """夜间每五分钟轮询至 23:00，并按失败情况做 5/10 分钟退避。"""
    backoff_seconds = ROUND_INTERVAL_SECONDS
    deadline = _night_deadline(dt.datetime.now())
    while True:
        now = dt.datetime.now()
        if not once and now >= deadline:
            logger.info("[night] 已到 23:00，夜间净值同步结束")
            return

        result = run_round("night", groups, now)
        if once:
            return

        fail_threshold = _threshold("DAILY_NAV_FAIL_THRESHOLD", DEFAULT_FAIL_THRESHOLD)
        network_threshold = _threshold(
            "DAILY_NAV_NETWORK_ERROR_THRESHOLD", DEFAULT_NETWORK_ERROR_THRESHOLD,
        )
        unhealthy = result.fail > fail_threshold or result.network_fail >= network_threshold
        if unhealthy:
            backoff_seconds = min(MAX_BACKOFF_SECONDS, backoff_seconds * 2)
            logger.warning(
                "[night] 失败偏多（失败=%d/阈值>%d，网络异常=%d/阈值>=%d），下轮等待 %d 秒",
                result.fail,
                fail_threshold,
                result.network_fail,
                network_threshold,
                backoff_seconds,
            )
        else:
            backoff_seconds = max(ROUND_INTERVAL_SECONDS, backoff_seconds // 2)
            logger.info("[night] 本轮健康，下轮等待 %d 秒", backoff_seconds)

        remaining = (deadline - dt.datetime.now()).total_seconds()
        if remaining <= 0:
            logger.info("[night] 本轮结束时已到 23:00，夜间净值同步结束")
            return
        time.sleep(min(backoff_seconds, remaining))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="基金净值日常增量同步")
    parser.add_argument(
        "--type-priority",
        default="",
        metavar="名单",
        help="类型优先级；默认 P1,P2,P3，可传 P1,P2,P3 或自定义类型清单",
    )
    parser.add_argument(
        "--round",
        choices=("auto", "night", "morning", "noon"),
        default="auto",
        help="运行轮次；默认按当前时间自动判断",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="只执行一轮；用于冒烟测试或手工补跑",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run_date = dt.date.today()
    configure_logging(run_date)

    with single_instance_lock() as acquired:
        if not acquired:
            return 0

        round_name = resolve_round(args.round)
        if round_name is None:
            logger.info("当前时间不在 night/morning/noon 调度窗口，退出")
            return 0
        if not is_trading_day(run_date):
            return 0

        groups = parse_type_priority(args.type_priority)
        logger.info(
            "[%s] 启动：优先级=%s，once=%s，pid=%d",
            round_name,
            "/".join(group.label for group in groups),
            args.once,
            os.getpid(),
        )
        if round_name == "night":
            run_night(groups, args.once)
        else:
            run_round(round_name, groups)
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        logger.warning("收到中断信号，退出")
        raise SystemExit(130)
