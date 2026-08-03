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


def _csv_values(values) -> list[str]:
    """把一个或多个逗号分隔参数归一化为非空字符串列表。"""
    if isinstance(values, str):
        values = [values]
    return [
        item.strip()
        for value in (values or [])
        for item in str(value).split(",")
        if item.strip()
    ]


def _non_negative_int(value: str) -> int:
    """解析允许为 0 的非负整数命令行参数。"""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是整数") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("必须是非负整数")
    return parsed


def _build_arg_parser(*, task_id_required: bool) -> argparse.ArgumentParser:
    """构造 worker CLI parser；兼容 task_runner 和直接模块启动。"""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "task_id",
        nargs=None if task_id_required else "?",
        type=int,
        help="已有 fetch_tasks 记录的 id；省略则由 main 创建任务",
    )
    parser.add_argument(
        "--task-id",
        dest="task_id_option",
        type=int,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--codes",
        nargs="+",
        default=None,
        help="基金代码（逗号分隔，也支持多个值）",
    )
    parser.add_argument(
        "--fund-types",
        "--types",
        nargs="+",
        default=None,
        dest="fund_types",
        help="基金类型（逗号分隔，也支持多个值）",
    )
    parser.add_argument("--limit", type=_non_negative_int, default=None)
    parser.add_argument("--incremental", action="store_true")
    # CLI 的 --json 由父命令消费；worker 接收时静默兼容即可。
    parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    return parser


def _parse_worker_namespace(argv, *, task_id_required: bool):
    parser = _build_arg_parser(task_id_required=task_id_required)
    ns = parser.parse_args(argv)
    if ns.task_id_option is not None:
        if ns.task_id is not None and ns.task_id != ns.task_id_option:
            parser.error("task_id 位置参数与 --task-id 不一致")
        ns.task_id = ns.task_id_option
    ns.codes = _csv_values(ns.codes)
    ns.fund_types = _csv_values(ns.fund_types)
    return ns


def parse_args(argv) -> tuple[int, list[str], list[str]]:
    """解析旧 worker 入口：``worker.py <task_id> [--codes] [--types]``。"""
    ns = _parse_worker_namespace(argv, task_id_required=True)
    return ns.task_id, ns.codes, ns.fund_types


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


def run_worker(
    task_id: int,
    codes: list[str],
    fund_types: list[str],
    process_one,
    *,
    incremental: bool = False,
    limit: int | None = None,
) -> None:
    """并发处理每只基金，持续更新进度，支持协作式终止。

    模块级回调走进程池；闭包或局部函数无法安全 pickle 时自动回退线程池，
    避免任务在提交阶段静默失败。
    """
    load_dotenv()
    targets = resolve_codes(codes, fund_types, incremental=incremental)
    if limit is not None:
        targets = targets[:limit]
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
                    {"success_count": success, "fail_count": fail, "current_count": current},
                )
    final_status = "terminated" if terminated else "completed"
    database.update(
        "fetch_tasks",
        {"id": task_id},
        {"status": final_status, "success_count": success, "fail_count": fail, "current_count": current},
    )
    logger.info("任务 %d %s：成功 %d，失败 %d", task_id, final_status, success, fail)


def _infer_task_type(process_one) -> str:
    """从 worker 模块推断直接启动时使用的 ``fetch_tasks.task_type``。"""
    module = getattr(process_one, "__module__", "")
    if module.startswith("app."):
        parts = module.split(".")
        if len(parts) > 1 and parts[1] != "common":
            return f"fetch_{parts[1]}"

    script_parts = os.path.normpath(sys.argv[0]).split(os.sep)
    try:
        app_index = len(script_parts) - 1 - script_parts[::-1].index("app")
    except ValueError:
        return "fetch_worker"
    if app_index + 1 < len(script_parts):
        return f"fetch_{script_parts[app_index + 1]}"
    return "fetch_worker"


def _new_task(process_one) -> int:
    """为无 task_id 的直接启动创建一条运行中的 fetch_tasks 记录。"""
    now = datetime.datetime.now().isoformat()
    task = database.insert("fetch_tasks", {
        "task_type": _infer_task_type(process_one),
        "status": "running",
        "created_at": now,
        "updated_at": now,
    })
    return int(task["id"])


def main(process_one) -> None:
    """worker 进程入口，兼容任务调度器和直接命令行启动。

    调度器传入位置 task_id 时复用已有任务；直接执行
    ``python -m app.<module>.fetch.worker --codes ...`` 时自动创建任务。
    ``run_worker`` 负责目标解析、并发处理、进度更新和正常/终止收尾，
    本函数补充启动初始化和异常收尾。
    """
    load_dotenv()
    args = _parse_worker_namespace(sys.argv[1:], task_id_required=False)
    task_id = args.task_id if args.task_id is not None else _new_task(process_one)

    if args.task_id is not None:
        database.update(
            "fetch_tasks",
            {"id": task_id},
            {"updated_at": datetime.datetime.now().isoformat()},
        )

    run_kwargs = {}
    if args.incremental:
        run_kwargs["incremental"] = True
    if args.limit is not None:
        run_kwargs["limit"] = args.limit

    try:
        run_worker(task_id, args.codes, args.fund_types, process_one, **run_kwargs)
    except Exception:
        try:
            database.update(
                "fetch_tasks",
                {"id": task_id},
                {"status": "failed", "updated_at": datetime.datetime.now().isoformat()},
            )
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception("任务 %d 异常后更新失败状态失败", task_id)
        raise


def _pool_init():
    """进程池初始化：加载环境变量，丢弃父进程 DB 单例让子进程自建连接。"""
    load_dotenv()
    database.reset_after_fork()
