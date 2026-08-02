"""worker 子进程共享主循环：确定基金集合 + 进程池并发 + 进度/协作式终止。

各模块 worker 只需提供 ``process_one(code) -> "success"|"skip"|"fail"``。
"""
from __future__ import annotations

import argparse
import datetime
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


def _previous_quarter(today: datetime.date) -> str:
    """返回上一个自然季度，形如 ``2026Q1``。"""
    current_quarter = (today.month - 1) // 3 + 1
    if current_quarter == 1:
        return f"{today.year - 1}Q4"
    return f"{today.year}Q{current_quarter - 1}"


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


def resolve_codes(
    codes: list[str], fund_types: list[str], incremental: bool = False,
) -> list[str]:
    """解析目标基金；支持全量、类型过滤和缺上一季度持仓的增量集合。"""
    if codes:
        return list(codes)

    if incremental:
        previous_quarter = _previous_quarter(datetime.date.today())
        covered_rows = database.select("fund_holdings", [
            ("quarter", f"eq.{previous_quarter}"),
            ("select", "fund_code"),
        ])
        covered_codes = {
            row["fund_code"] for row in covered_rows if row.get("fund_code")
        }
        params: list[tuple[str, str]] = [
            ("select", "code"),
            ("type", "not.ilike.货币型*"),
        ]
        if fund_types:
            params.append(("type", f"in.({','.join(fund_types)})"))
        return [
            row["code"]
            for row in database.select("funds", params)
            if row["code"] not in covered_codes
        ]

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
    """读取进程池并发数；无效值回退到保守默认值。上限 64 防止资源耗尽。"""
    try:
        return min(64, max(1, int(os.getenv("IFUND_WORKER_CONCURRENCY", str(DEFAULT_CONCURRENCY)))))
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
        executor_kwargs["initializer"] = _pool_init
    with executor_cls(**executor_kwargs) as executor:
        future_map = {}
        for code in targets:
            if _is_terminated(task_id):
                terminated = True
                break
            future = executor.submit(_safe_process, process_one, code)
            future_map[future] = code
        if terminated:
            for pending in list(future_map):
                code = future_map[pending]
                cancelled = pending.cancel()
                if cancelled:
                    del future_map[pending]
                logger.info(
                    "任务终止，基金 %s 的 future.cancel() 返回 %s（%s）",
                    code,
                    cancelled,
                    "已取消" if cancelled else "已运行或已完成",
                )
        for future in as_completed(future_map):
            code = future_map[future]
            try:
                _, status = future.result()
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.exception("处理基金 %s 异常：%s", code, exc)
                status = "fail"
            if status == "success":
                success += 1
            elif status == "fail":
                fail += 1
            current += 1
            if not _is_terminated(task_id):
                database.update(
                    "fetch_tasks",
                    {"id": task_id},
                    {"success": success, "fail": fail, "current": current},
                )
    final_status = "terminated" if terminated else "completed"
    database.update(
        "fetch_tasks",
        {"id": task_id},
        {"status": final_status, "success": success, "fail": fail, "current": current},
    )
    logger.info("任务 %d %s：成功 %d，失败 %d", task_id, final_status, success, fail)


def _pool_init():
    """进程池初始化：加载环境变量和数据库连接。"""
    load_dotenv()
    database.init_app(None)
