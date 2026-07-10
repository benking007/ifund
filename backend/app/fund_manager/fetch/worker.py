#!/usr/bin/env python3
"""fund_manager_tenure worker：解析东财 F10「基金经理」页（jjjl_CODE.html）的任职史。

雪球四接口只给「当前经理名」，无法回答「3y/5y 业绩是不是当前经理做的、中途换没换人」。
东财 F10 页第一张表即本基金历任+现任经理任职区间：
    起始期 | 截止期 | 基金经理 | 任职期间 | 任职回报
逐段落到 fund_manager_tenure（seq=0 为最新一段/含「至今」）。纯 requests+正则，不依赖 bs4/akshare。
"""
from __future__ import annotations

import datetime
import os
import re
import sys
from pathlib import Path

_BACKEND_DIR = os.getenv("IFUND_BACKEND_DIR") or str(Path(__file__).resolve().parents[3])
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)
os.chdir(_BACKEND_DIR)

# pylint: disable=wrong-import-position
import requests  # pylint: disable=import-error

from app import db as database
from app.common import worker_base

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
# 任职期间文本 "4年又148天" / "1年又16天" / "148天" → 天数
_YEAR_RE = re.compile(r"(\d+)\s*年")
_DAY_RE = re.compile(r"(\d+)\s*天")


def _strip_tags(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html)).strip()


def _parse_days(text: str) -> int | None:
    """"4年又148天" → 4*365+148。取近似（年按 365 天），仅用于粗排任职长短。"""
    if not text:
        return None
    y = _YEAR_RE.search(text)
    d = _DAY_RE.search(text)
    if not y and not d:
        return None
    return (int(y.group(1)) * 365 if y else 0) + (int(d.group(1)) if d else 0)


def _parse_return(text: str) -> float | None:
    if not text:
        return None
    m = _NUM_RE.search(text.replace(",", ""))
    return float(m.group()) if m else None


def _extract_rows(html: str) -> list[list[str]]:
    """定位含表头「起始期…基金经理…任职回报」的那张表，抽出各任职段行（5 列）。"""
    # 按 <table> 切块，找含关键表头的块（避免误取下方「现任经理管理的其它基金」表）
    for block in re.split(r"<table", html)[1:]:
        block = "<table" + block.split("</table>")[0]
        if "起始期" not in block or "任职回报" not in block:
            continue
        rows: list[list[str]] = []
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", block, re.S):
            cells = [_strip_tags(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
            # 任职段行：5 列且首列是日期
            if len(cells) == 5 and _DATE_RE.fullmatch(cells[0] or ""):
                rows.append(cells)
        if rows:
            return rows
    return []


def _fetch_html(code: str) -> str | None:
    url = f"https://fundf10.eastmoney.com/jjjl_{code}.html"
    headers = {"User-Agent": _UA, "Referer": url}
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text if resp.status_code == 200 else None
    except Exception:  # pylint: disable=broad-exception-caught
        return None


def _is_fresh(code: str) -> bool:
    """今天已拉过则跳过（经理任职变动罕见，按自然日缓存即可）。"""
    rows = database.select("fund_manager_tenure",
                           {"fund_code": f"eq.{code}", "limit": 1, "order": "seq.asc"})
    if not rows:
        return False
    ft = (rows[0].get("fetch_time") or "")[:10]
    return ft == datetime.date.today().isoformat()


def _process_one(code: str):
    if _is_fresh(code):
        return "skip"
    html = _fetch_html(code)
    if not html:
        return "fail"
    raw_rows = _extract_rows(html)
    if not raw_rows:
        # 页面可达但无任职表（新基金/结构特殊）：写一条空占位，避免每次重拉
        return "fail"
    now = datetime.datetime.now().isoformat()
    payload = []
    for seq, (start, end, managers, tenure_text, ret) in enumerate(raw_rows):
        is_current = 1 if ("至今" in end or not _DATE_RE.fullmatch(end or "")) else 0
        payload.append({
            "fund_code": code,
            "seq": seq,
            "start_date": start or None,
            "end_date": None if is_current else (end or None),
            "is_current": is_current,
            "managers": (managers or "").strip(),
            "tenure_text": tenure_text or None,
            "tenure_days": _parse_days(tenure_text),
            "tenure_return": _parse_return(ret),
            "fetch_time": now,
        })
    database.delete("fund_manager_tenure", {"fund_code": f"eq.{code}"})
    database.batch_insert("fund_manager_tenure", payload)
    return "success"


if __name__ == "__main__":
    worker_base.main(_process_one)
