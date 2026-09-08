#!/usr/bin/env python3
"""生产交易日历低频同步：复用 API/CLI 服务层并保存 JSON 证据。"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)

from app.trade_calendar import service  # pylint: disable=wrong-import-position

LOG_DIR = BACKEND_DIR / "logs" / "trade_calendar_sync"
logger = logging.getLogger("sync_trade_calendar")


def atomic_json(path: Path, payload: dict) -> None:
    """原子保存同步证据。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def configure_logging() -> None:
    """日志同时进入 cron 重定向和标准输出。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, default=LOG_DIR)
    return parser


def main(argv: list[str] | None = None) -> int:
    """执行同步；失败返回 2，便于 cron 捕获。"""
    args = build_parser().parse_args(argv)
    configure_logging()
    started = dt.datetime.now().astimezone()
    try:
        if os.getenv("DB_BACKEND", "").strip().lower() != "mysql":
            raise RuntimeError("自动交易日历同步仅允许 DB_BACKEND=mysql")
        result = service.sync_calendar()
        payload = {
            "ok": True,
            "started_at": started.isoformat(timespec="seconds"),
            "finished_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            **result,
        }
        directory = args.evidence_dir.resolve()
        stamp = started.strftime("%Y%m%dT%H%M%S%z")
        evidence_path = directory / f"trade-calendar-{stamp}.json"
        atomic_json(evidence_path, payload)
        atomic_json(directory / "latest.json", payload)
        if result["missing_years"]:
            logger.warning(
                "Tushare 尚未发布完整远期日历：missing_years=%s；月度任务将继续探测",
                result["missing_years"],
            )
        logger.info(
            "交易日历同步完成：source=%s exchange=%s window=%s~%s count=%d "
            "latest=%s tushare_calls=%d evidence=%s",
            result["source"],
            result["exchange"],
            result["window_start"],
            result["window_end"],
            result["count"],
            result["latest"],
            result["tushare_calls"],
            evidence_path,
        )
        return 0
    except Exception as exc:  # pylint: disable=broad-exception-caught
        payload = {
            "ok": False,
            "started_at": started.isoformat(timespec="seconds"),
            "failed_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "error_type": type(exc).__name__,
            "error": str(exc)[:1000],
        }
        atomic_json(args.evidence_dir.resolve() / "latest.json", payload)
        logger.exception("交易日历同步失败")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
