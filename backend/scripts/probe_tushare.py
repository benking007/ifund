#!/usr/bin/env python3
"""Tushare 探测：经共享总闸调用 fund_div 一次。供 bgjob 脚本判断恢复。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from app.fund_nav.fetch import tushare_client  # pylint: disable=wrong-import-position


def main() -> str:
    """执行一次低成本 fund_div 探测并返回稳定的 OK/FAIL 文本。"""
    if not tushare_client.get_token():
        return "FAIL(no token)"
    try:
        rows = tushare_client.fetch_fund_div("519981.OF", timeout=(5, 20))
        return f"OK(items={len(rows)})"
    except tushare_client.TushareError as exc:
        return f"FAIL({type(exc).__name__})"


if __name__ == "__main__":
    result = main()
    print(result)
    sys.exit(0 if result.startswith("OK") else 1)
