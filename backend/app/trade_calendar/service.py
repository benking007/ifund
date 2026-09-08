"""交易日历同步服务，供 API、CLI 与自动任务复用。"""

from __future__ import annotations

from app.trade_calendar.crud import calendar_crud
from app.trade_calendar.fetch import fetcher


def sync_calendar() -> dict:
    """拉取并原子替换统一交易日历，返回审计摘要。"""
    existing = calendar_crud.list_dates()
    fetched = fetcher.fetch_trade_calendar(existing_dates=existing)
    if not fetched.dates:
        raise RuntimeError("交易日历拉取结果为空，拒绝替换")
    count = calendar_crud.replace_all(fetched.dates)
    return {
        "count": count,
        "earliest": fetched.dates[0],
        "latest": fetched.dates[-1],
        **fetched.metadata(),
    }
