"""季报披露日期映射：给定买入日期，返回当时已公开的最新季报 quarter 字符串。

A股基金季报披露截止：
  Q1 (3/31数据) → 4/30 前公开
  Q2 (6/30数据) → 8/31 前公开
  Q3 (9/30数据) → 10/31 前公开
  Q4 (12/31数据) → 次年 3/31 前公开
"""
from __future__ import annotations

from datetime import date


def latest_disclosed_quarter(buy_date: str) -> str:
    d = date.fromisoformat(buy_date)
    y, m = d.year, d.month

    if m >= 11:
        return f"{y}Q3"
    if m >= 9:
        return f"{y}Q2"
    if m >= 5:
        return f"{y}Q1"
    if m >= 4:
        return f"{y - 1}Q4"
    return f"{y - 1}Q3"
