"""worker 子进程共享主循环：确定基金集合 + 进程池并发 + 进度/协作式终止。

各模块 worker 只需提供 ``process_one(code) -> "success"|"skip"|"fail"``。
"""
from __future__ import annotations

import argparse
import logging
import math
import os
import pickle
import random
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed

from dotenv import load_dotenv

from app import db as database

logger = logging.getLogger(__name__)

DEFAULT_CONCURRENCY = 4
MAX_RETRIES = 4
RETRY_JITTER = 0.2


def safe_float(value):
    """把值转 float；NaN/None/非数返回 None。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(num) else num


def parse_args(argv) -> tuple[int, list[str], list[str]]:
    """解析 ``worker.py <task_id> [--codes a,b] [--fund-types x,y]``。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("task_id", type=int)
    parser.add_argument("--codes", default="")
    parser.add_argument("--fund-types", default="", dest="fund_types")
    ns = parser.parse_args(argv)
    codes = [c for c in ns.codes.split(",") if c]
    fund_types = [t for t in ns.fund_types.split(",") if t]
    return ns.task_id, codes, fund_types


def resolve_codes(codes: list[str], fund_types: list[str]) -> list[str]:
    """--codes 优先；否则按 --fund-types 查 funds；否则全量。"""
    if codes:
        return list(codes)
    params = None
    if fund_types:
        params = {"type": f"in.({','.join(fund_types)})"}
    return [r["code"] for r in database.select("funds", params)]


def _is_terminated(task_id: int) -> bool:
    task = database.select_one("fetch_tasks", {"id": f"eq.{task_id}"})
    return task is None or task.get("status") == "terminated"


def _safe_process(process_one, code: str) -> tuple[str, str]:
    """在池内执行单只基金；失败指数退避，且不写任务进度表。"""
    for attempt in range(MAX_RETRIES + 1):
        try:
            return code, process_one(code) or "success"
        except Exception as exc:  # pylint: disable=broad-exception-caught
            if attempt == MAX_RETRIES:
                logger.exception("处理基金 %s 失败，已重试 %d 次", code, attempt)
                return code, "fail"
            logger.warning(
                "处理基金 %s 失败，将在第 %d 次重试：%s", code, attempt + 1, exc,
            )
            delay = (2 ** attempt) * random.uniform(1 - RETRY_JITTER, 1 + RETRY_JITTER)
            time.sleep(delay)
    return code, "fail"  # pragma: no cover - 循环必定在上方返回


def _worker_concurrency() -> int:
    """读取进程池并发数；无效值回退到保守默认值。"""
    try:
        return max(1, int(os.getenv("IFUND_WORKER_CONCURRENCY", str(DEFAULT_CONCURRENCY))))
    except ValueError:
        return DEFAULT_CONCURRENCY


def _is_pickleable(process_one) -> bool:
    """检查回调能否交给 ProcessPoolExecutor；闭包/局部函数不可用时降级线程池。"""
    try:
        pickle.dumps(process_one)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("process_one 不可 pickle，降级为线程池：%s", exc)
        return False
    return True


def run_worker(task_id: int, codes: list[str], fund_types: list[str], process_one) -> None:
    """并发处理每只基金，持续更新进度，支持协作式终止。

    模块级回调走进程池；闭包或局部函数无法安全 pickle 时自动回退线程池，
    避免任务在提交阶段静默失败。
    """
    load_dotenv()
    targets = resolve_codes(codes, fund_types)
    concurrency = _worker_concurrency()
    database.update("fetch_tasks", {"id": task_id}, {"target_count": len(targets)})
    success = fail = current = 0
    terminated = False
    executor_cls = ProcessPoolExecutor if _is_pickleable(process_one) else ThreadPoolExecutor
    executor_kwargs = {"max_workers": concurrency}
    if executor_cls is ProcessPoolExecutor:
        executor_kwargs["initializer"] = database.reset_after_fork
    with executor_cls(**executor_kwargs) as executor:
        futures = {executor.submit(_safe_process, process_one, code): code for code in targets}
        for future in as_completed(futures):
            current += 1
            try:
                _, result = future.result()
            except Exception:  # pylint: disable=broad-exception-caught
                logger.exception("worker 子任务失败：%s", futures[future])
                result = "fail"
            if result == "fail":
                fail += 1
            else:
                success += 1
            database.update("fetch_tasks", {"id": task_id}, {
                "current_count": current, "success_count": success, "fail_count": fail,
            })
            if _is_terminated(task_id):
                terminated = True
                for pending in futures:
                    pending.cancel()
                break
    database.update("fetch_tasks", {"id": task_id},
                    {"status": "terminated" if terminated else "finished"})


def main(process_one) -> None:
    """worker 入口：解析 argv 并跑主循环。"""
    task_id, codes, fund_types = parse_args(sys.argv[1:])
    run_worker(task_id, codes, fund_types, process_one)
