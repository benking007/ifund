#!/usr/bin/env python3
"""申万三级行业映射 worker：遍历 336 个三级行业（legulegu），逐行业拉成分股。

legulegu 对高频连续请求限流严重，故本 worker 串行 + 限速 + 退避重试，并支持**续采**：
已采到记录的三级行业整体跳过，重跑只补未采/失败的行业（配合页面可分批慢采）。
成分股的申万一/二级由 ``sw_index_third_info`` 的「上级行业」链回溯得到。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_BACKEND_DIR = os.getenv("IFUND_BACKEND_DIR") or str(Path(__file__).resolve().parents[3])
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)
os.chdir(_BACKEND_DIR)

# pylint: disable=wrong-import-position
import time
from io import StringIO

import akshare as ak  # pylint: disable=import-error
import akshare.index.index_sw as _index_sw  # pylint: disable=import-error
import pandas as pd  # pylint: disable=import-error
import requests  # pylint: disable=import-error

from app import db as database
from app.common import worker_base
from app.common.network import install_module_timeout
from app.stock_industry.crud import industry_crud

SLEEP_SEC = 2.0          # 行业间隔，规避 legulegu 限流
RETRY = 3
HDR = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}

install_module_timeout(_index_sw)


def _fetch_cons(industry_code: str):
    """拉单个三级行业成分股 DataFrame；多次退避重试后仍失败返回 None。"""
    url = f"https://legulegu.com/stockdata/index-composition?industryCode={industry_code}"
    for attempt in range(RETRY):
        try:
            resp = requests.get(url, headers=HDR, timeout=25)
            return pd.read_html(StringIO(resp.text))[0]
        except Exception:  # pylint: disable=broad-exception-caught
            time.sleep(1.5 * (attempt + 1))
    return None


def _log(msg: str) -> None:
    """worker 子进程 stderr 被父进程 DEVNULL 吞掉，关键诊断信息单独落文件。"""
    path = Path(_BACKEND_DIR) / "logs" / "industry_worker.log"
    path.parent.mkdir(exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"[sw] {msg}\n")


def _is_terminated(task_id: int) -> bool:
    task = database.select_one("fetch_tasks", {"id": f"eq.{task_id}"})
    return task is None or task.get("status") == "terminated"


def _l2_to_l1() -> dict:
    """二级→一级名映射；legulegu 该接口已随页面改版失效，失败则返回空（一级留空）。"""
    try:
        second = ak.sw_index_second_info()
        return dict(zip(second["行业名称"], second["上级行业"]))
    except Exception as exc:  # pylint: disable=broad-exception-caught
        _log(f"二级目录获取失败（一级将留空）：{exc}")
        return {}


def _save_cons(frame, sw_chain) -> None:
    """把一个行业的成分股写入映射表（保留东财字段、跳过人工修正）。"""
    for raw_code, raw_name in zip(frame["股票代码"], frame["股票简称"]):
        code = str(raw_code).split(".", maxsplit=1)[0].strip()
        if not code:
            continue
        industry_crud.upsert_industry(
            code, str(raw_name).strip(),
            market=industry_crud.classify_market(code), sw=sw_chain, source="legulegu",
        )


def run(task_id: int, only_codes: list[str]) -> None:
    """主循环：遍历三级行业，续采跳过已采行业，持续更新任务进度，支持协作式终止。

    顶层捕获所有异常：失败时落日志并把任务置 terminated，避免卡在 running / 0/0。
    """
    try:
        third = ak.sw_index_third_info()
    except Exception as exc:  # pylint: disable=broad-exception-caught
        _log(f"获取申万三级目录失败（legulegu 不可达？）：{exc}")
        database.update("fetch_tasks", {"id": task_id}, {"status": "terminated"})
        return

    targets = [(str(r["行业代码"]), r["行业名称"], r["上级行业"]) for _, r in third.iterrows()]
    if only_codes:
        wanted = set(only_codes)
        targets = [t for t in targets if t[0] in wanted]
    database.update("fetch_tasks", {"id": task_id}, {"target_count": len(targets)})

    l2_to_l1 = _l2_to_l1()
    covered = industry_crud.sw_covered_l3()
    success = fail = current = 0
    terminated = False
    for icode, l3name, l2name in targets:
        current += 1
        if l3name in covered:                       # 续采：已采行业整体跳过
            success += 1
        else:
            frame = _fetch_cons(icode)
            if frame is None:
                fail += 1
            else:
                _save_cons(frame, (l2_to_l1.get(l2name, ""), l2name, l3name))
                success += 1
            time.sleep(SLEEP_SEC)
        database.update("fetch_tasks", {"id": task_id}, {
            "current_count": current, "success_count": success, "fail_count": fail,
        })
        if _is_terminated(task_id):
            terminated = True
            break
    database.update("fetch_tasks", {"id": task_id},
                    {"status": "terminated" if terminated else "finished"})


if __name__ == "__main__":
    _task_id, _codes, _ = worker_base.parse_args(sys.argv[1:])
    try:
        run(_task_id, _codes)
    except Exception as _exc:  # pylint: disable=broad-exception-caught
        _log(f"worker 异常退出：{_exc}")
        database.update("fetch_tasks", {"id": _task_id}, {"status": "terminated"})
        raise
