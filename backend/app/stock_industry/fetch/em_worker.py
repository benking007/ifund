#!/usr/bin/env python3
"""东财兜底 worker：校正海外误判 + 补北交所/A 股缺口 + 补港股行业。

申万体系只含 A 股。本 worker 做三件事：
1. **校正市场**：用 A 股全集（``stock_info_a_code_name``）把「6 位数字但不在全集」的持仓
   （韩股如 005930 三星、或已退市）改判 market=OTHER（归海外/其他）；
2. **补 A 股缺口**：北交所 920 段及申万成分缺失的 A 股优先调用
   ``stock_individual_info_em``，失败时回退巨潮行业变更接口；写入 ``source='em'``。
3. **补港股**：``stock_hk_company_profile_em`` 取「所属行业」写 em_industry（datacenter 域名，可直连）。

``stock_industry_category_cninfo`` 只返回行业目录，不含股票代码；因此 A 股缺口按股票
逐只查询行业。巨潮接口作为东财 push2 不可达时的 akshare 内置兜底，仍不引入新依赖。
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
import datetime
import time

import akshare as ak  # pylint: disable=import-error
from akshare.stock import stock_industry_cninfo as _cninfo_api  # pylint: disable=import-error

from app import db as database
from app.common import worker_base
from app.stock_industry.crud import industry_crud

SLEEP_SEC = 0.6
INDIVIDUAL_TIMEOUT = 3.0
CNINFO_TIMEOUT = 8.0
CNINFO_START_DATE = "20000101"


def _log(msg: str) -> None:
    """worker 子进程 stderr 被父进程 DEVNULL 吞掉，关键诊断信息单独落文件。"""
    path = Path(_BACKEND_DIR) / "logs" / "industry_worker.log"
    path.parent.mkdir(exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"[em] {msg}\n")


def _a_master() -> set[str]:
    """A 股代码全集（用于把误判成 A 股的韩股/退市剔出）；失败返回空集（则不校正）。"""
    try:
        return set(ak.stock_info_a_code_name()["code"].astype(str))
    except Exception as exc:  # pylint: disable=broad-exception-caught
        _log(f"获取 A 股全集失败（本轮跳过市场校正）：{exc}")
        return set()


def _hk_industry(code: str) -> str:
    """港股「所属行业」：东财港股公司资料（宽表单行，取「所属行业」列）。"""
    try:
        frame = ak.stock_hk_company_profile_em(symbol=code)
    except Exception:  # pylint: disable=broad-exception-caught
        return ""
    if frame is None or frame.empty or "所属行业" not in frame.columns:
        return ""
    return str(frame["所属行业"].iloc[0] or "").strip()


def _clean_value(value) -> str:
    """把 akshare DataFrame 中的 NaN/空占位统一成空字符串。"""
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"", "nan", "none", "nat", "-", "--"} else text


def _industry_from_info_frame(frame) -> str:
    """从 ``stock_individual_info_em`` 的 item/value 宽松提取「行业」。"""
    if frame is None or getattr(frame, "empty", True):
        return ""
    columns = {str(column) for column in getattr(frame, "columns", [])}
    if "行业" in columns:
        return _clean_value(frame["行业"].iloc[0])
    if {"item", "value"}.issubset(columns):
        for row in frame.to_dict("records"):
            if _clean_value(row.get("item")) == "行业":
                return _clean_value(row.get("value"))
    for row in frame.itertuples(index=False, name=None):
        if len(row) >= 2 and _clean_value(row[0]) == "行业":
            return _clean_value(row[1])
    return ""


def _cninfo_industry(code: str) -> str:
    """个股接口不可达时，从巨潮行业变更记录取最近的非空行业名。"""
    original_post = _cninfo_api.requests.post

    def post_with_timeout(*args, **kwargs):
        kwargs.setdefault("timeout", CNINFO_TIMEOUT)
        return original_post(*args, **kwargs)

    try:
        # akshare 当前接口没有暴露 timeout；只在本次调用临时加默认超时，避免
        # 单只失联证券把整个补采任务无限挂住。worker 是独立进程，恢复全局引用。
        _cninfo_api.requests.post = post_with_timeout
        frame = ak.stock_industry_change_cninfo(
            symbol=code,
            start_date=CNINFO_START_DATE,
            end_date=datetime.date.today().strftime("%Y%m%d"),
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        _log(f"巨潮行业兜底失败 {code}: {exc}")
        return ""
    finally:
        _cninfo_api.requests.post = original_post
    if frame is None or getattr(frame, "empty", True):
        return ""
    records = frame.to_dict("records")
    # 中类通常最接近东财「行业」字段；若为空再逐级退到大类/门类。
    for column in ("行业中类", "行业大类", "行业次类", "行业门类"):
        candidates = [
            (str(row.get("变更日期") or ""), _clean_value(row.get(column)))
            for row in records
        ]
        candidates = [item for item in candidates if item[1]]
        if candidates:
            return max(candidates, key=lambda item: item[0])[1]
    return ""


def _a_industry(code: str) -> str:
    """A 股/北交所行业：东财个股信息优先，巨潮行业变更记录兜底。"""
    try:
        frame = ak.stock_individual_info_em(symbol=code, timeout=INDIVIDUAL_TIMEOUT)
        industry = _industry_from_info_frame(frame)
        if industry:
            return industry
    except TypeError:
        # 兼容旧版 akshare 或测试替身没有 timeout 参数的签名。
        try:
            industry = _industry_from_info_frame(ak.stock_individual_info_em(symbol=code))
            if industry:
                return industry
        except Exception as exc:  # pylint: disable=broad-exception-caught
            _log(f"东财个股行业失败 {code}: {exc}")
    except Exception as exc:  # pylint: disable=broad-exception-caught
        _log(f"东财个股行业失败 {code}: {exc}")
    return _cninfo_industry(code)


def _is_terminated(task_id: int) -> bool:
    task = database.select_one("fetch_tasks", {"id": f"eq.{task_id}"})
    return task is None or task.get("status") == "terminated"


def _reclassify_overseas(held, names) -> int:
    """把「6 位数字但不在 A 股全集」的持仓改判海外（韩股/退市）；离线，不计采集进度。"""
    master = _a_master()
    if not master:
        return 0
    moved = 0
    for code in industry_crud.uncovered_held(held, markets=("A",)):
        if code not in master:
            industry_crud.upsert_industry(code, names.get(code, ""), market="OTHER")
            moved += 1
    return moved


def run(task_id: int) -> None:
    """先校正海外误判，再补未覆盖 A 股/北交所与港股的行业。"""
    held = industry_crud.held_codes()
    names = industry_crud.held_names()

    moved = _reclassify_overseas(held, names)
    a_targets = industry_crud.uncovered_held(held, markets=("A",))
    hk_targets = industry_crud.uncovered_held(held, markets=("HK",))
    targets = a_targets + hk_targets
    bj_count = sum(industry_crud.is_bj_stock(code) for code in a_targets)
    _log(
        f"改判海外 {moved} 只；待补 A 股 {len(a_targets)} 只"
        f"（北交所 {bj_count}）；待补港股 {len(hk_targets)} 只"
    )

    database.update("fetch_tasks", {"id": task_id}, {"target_count": len(targets)})
    success = fail = current = 0
    terminated = False
    for code in a_targets:
        current += 1
        industry = _a_industry(code)
        if industry:
            industry_crud.upsert_industry(
                code,
                names.get(code, ""),
                market="BJ" if industry_crud.is_bj_stock(code) else "A",
                em=industry,
                source="em",
            )
            success += 1
        else:
            fail += 1
        database.update("fetch_tasks", {"id": task_id}, {
            "current_count": current, "success_count": success, "fail_count": fail,
        })
        if _is_terminated(task_id):
            terminated = True
            break
        time.sleep(SLEEP_SEC)
    if not terminated:
        for code in hk_targets:
            current += 1
            industry = _hk_industry(code)
            if industry:
                industry_crud.upsert_industry(
                    code, names.get(code, ""), market="HK", em=industry, source="eastmoney")
                success += 1
            else:
                fail += 1
            database.update("fetch_tasks", {"id": task_id}, {
                "current_count": current, "success_count": success, "fail_count": fail,
            })
            if _is_terminated(task_id):
                terminated = True
                break
            time.sleep(SLEEP_SEC)
    database.update("fetch_tasks", {"id": task_id},
                    {"status": "terminated" if terminated else "finished"})


if __name__ == "__main__":
    _task_id, _, _ = worker_base.parse_args(sys.argv[1:])
    try:
        run(_task_id)
    except Exception as _exc:  # pylint: disable=broad-exception-caught
        _log(f"worker 异常退出：{_exc}")
        database.update("fetch_tasks", {"id": _task_id}, {"status": "terminated"})
        raise
