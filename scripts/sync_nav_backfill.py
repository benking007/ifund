#!/usr/bin/env python3
"""净值补缺分批拉取：按基准交易日 2026-07-31，增量 + 全量分批。

用法: backend/venv/bin/python scripts/sync_nav_backfill.py [--incr-only|--full-only]
清单文件: /tmp/nav_incr.txt (有记录滞后), /tmp/nav_full.txt (无记录)
每批 BATCH_SIZE 只，串行批次、批内进程池并发（IFUND_WORKER_CONCURRENCY）。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

from app.fund_nav.fetch.worker import _process_one  # noqa: E402
from app.common import worker_base  # noqa: E402
from app import db  # noqa: E402

BATCH_SIZE = int(os.environ.get("NAV_BATCH_SIZE", "1500"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/root/workspace/ifund/logs/nav_backfill.log"),
    ],
)
log = logging.getLogger("nav_backfill")


def load_codes(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip()]


def run_batch(codes: list[str], label: str, batch_no: int, total_batches: int) -> dict:
    db.delete("fetch_tasks", {"task_type": f"nav_backfill_{label}"})
    row_ins = db.insert("fetch_tasks", {
        "task_type": f"nav_backfill_{label}",
        "status": "running",
        "target_count": 0,
        "success_count": 0,
        "fail_count": 0,
        "current_count": 0,
    })
    task_id = row_ins["id"]
    t0 = time.monotonic()
    worker_base.run_worker(task_id, codes, [], _process_one)
    row = db.select_one("fetch_tasks", {"id": task_id})
    elapsed = time.monotonic() - t0
    counts = {
        "success": row["success_count"],
        "fail": row["fail_count"],
        "skip": row.get("skip_count", 0),
    }
    log.info("[%s] 批次 %d/%d: %d 只, %.1fs, success=%d fail=%d skip=%d",
             label, batch_no, total_batches, len(codes), elapsed,
             counts["success"], counts["fail"], counts["skip"])
    return counts


def run_phase(path: str, label: str, grand: dict) -> None:
    codes = load_codes(path)
    log.info("阶段 %s: %d 只", label, len(codes))
    if not codes:
        return
    total = (len(codes) + BATCH_SIZE - 1) // BATCH_SIZE
    for i in range(total):
        batch = codes[i * BATCH_SIZE:(i + 1) * BATCH_SIZE]
        c = run_batch(batch, label, i + 1, total)
        for k in grand:
            grand[k] += c[k]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--incr-only", action="store_true")
    ap.add_argument("--full-only", action="store_true")
    args = ap.parse_args()

    grand = {"success": 0, "fail": 0, "skip": 0}
    t_start = time.monotonic()
    log.info("=== 净值补缺开始 batch_size=%d ===", BATCH_SIZE)

    if not args.full_only:
        run_phase("/tmp/nav_incr.txt", "incr", grand)
    if not args.incr_only:
        run_phase("/tmp/nav_full.txt", "full", grand)

    elapsed = time.monotonic() - t_start
    log.info("=== 完成: %.1f min, success=%d fail=%d skip=%d ===",
             elapsed / 60, grand["success"], grand["fail"], grand["skip"])


if __name__ == "__main__":
    main()
