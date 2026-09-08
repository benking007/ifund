"""交易日历拉取：Tushare ``trade_cal`` 主源，AkShare/Sina 失败兜底。"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import asdict, dataclass, field

from app.fund_nav.fetch import tushare_client

logger = logging.getLogger(__name__)

# 沪深交易所遵循相同的法定休市安排；统一表没有 exchange 字段，因此固定以 SSE
# 作为法定交易日口径。滚动九年窗口在 2026 年正好覆盖 2020-01-01~2028-12-31，
# 每年一次请求可规避 trade_cal 单次行数上限，也把每轮调用稳定控制为 9 次。
TUSHARE_EXCHANGE = "SSE"
LOOKBACK_YEARS = 6
LOOKAHEAD_YEARS = 2
TUSHARE_FIELDS = "cal_date,is_open"


@dataclass(frozen=True)
class CalendarFetchResult:
    """一次日历拉取的可审计结果。"""

    dates: list[str]
    source: str
    exchange: str
    window_start: str
    window_end: str
    tushare_calls: int
    preserved_outside_window: int
    fallback_reason: str | None = None
    year_counts: dict[str, int] = field(default_factory=dict)
    missing_years: list[int] = field(default_factory=list)
    preserved_missing_year_dates: int = 0

    def metadata(self) -> dict:
        """返回不含日期明细的紧凑元数据。"""
        payload = asdict(self)
        payload.pop("dates")
        return payload


class TushareCalendarError(RuntimeError):
    """携带失败前调用量，便于共享总闸审计。"""

    def __init__(self, message: str, calls: int):
        super().__init__(message)
        self.calls = calls


def default_window(today: dt.date | None = None) -> tuple[str, str]:
    """返回滚动九年窗口；当前 2026 年为 2020~2028。"""
    anchor = today or dt.datetime.now().astimezone().date()
    return (
        dt.date(anchor.year - LOOKBACK_YEARS, 1, 1).isoformat(),
        dt.date(anchor.year + LOOKAHEAD_YEARS, 12, 31).isoformat(),
    )


def _iso_date(value: object) -> str:
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return dt.date.fromisoformat(text).isoformat()


def _year_windows(start_date: str, end_date: str) -> list[tuple[str, str]]:
    start = dt.date.fromisoformat(start_date)
    end = dt.date.fromisoformat(end_date)
    if end < start:
        raise ValueError("交易日历结束日不能早于开始日")
    return [
        (
            max(start, dt.date(year, 1, 1)).isoformat(),
            min(end, dt.date(year, 12, 31)).isoformat(),
        )
        for year in range(start.year, end.year + 1)
    ]


def fetch_tushare_trade_dates(
    start_date: str,
    end_date: str,
    *,
    exchange: str = TUSHARE_EXCHANGE,
) -> tuple[list[str], int]:
    """按年调用 Tushare trade_cal，只返回开市日。"""
    dates: set[str] = set()
    calls = 0
    for page_start, page_end in _year_windows(start_date, end_date):
        try:
            calls += 1
            rows = tushare_client.call(
                "trade_cal",
                {
                    "exchange": exchange,
                    "start_date": page_start.replace("-", ""),
                    "end_date": page_end.replace("-", ""),
                    "is_open": "1",
                },
                TUSHARE_FIELDS,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            raise TushareCalendarError(
                f"Tushare trade_cal {page_start[:4]} 拉取失败: {type(exc).__name__}: {exc}",
                calls,
            ) from exc
        for row in rows:
            if str(row.get("is_open", "1")) not in {"1", "1.0", "True", "true"}:
                continue
            try:
                day = _iso_date(row.get("cal_date"))
            except (TypeError, ValueError):
                continue
            if start_date <= day <= end_date:
                dates.add(day)
    if not dates:
        raise TushareCalendarError("Tushare trade_cal 返回空交易日历", calls)
    return sorted(dates), calls


def fetch_sina_trade_dates() -> list[str]:
    """旧 Sina/AkShare 全历史交易日源，仅在 Tushare 失败时使用。"""
    # 只在主源失败时承担兜底，避免正常进程支付 AkShare 的重导入成本。
    # pylint: disable=import-outside-toplevel
    import akshare as ak  # pylint: disable=import-outside-toplevel,import-error
    from akshare.tool import (
        trade_date_hist,  # pylint: disable=import-outside-toplevel,import-error
    )

    from app.common.network import (
        install_module_timeout,  # pylint: disable=import-outside-toplevel
    )

    install_module_timeout(trade_date_hist)
    frame = ak.tool_trade_date_hist_sina()
    dates = []
    for value in frame["trade_date"].tolist():
        dates.append(str(value)[:10])
    return sorted(set(dates))


def fetch_trade_calendar(
    *,
    existing_dates: list[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> CalendarFetchResult:
    """拉取主窗口并保留窗口外历史；Tushare 失败时无损降级。"""
    default_start, default_end = default_window()
    start_date = start_date or default_start
    end_date = end_date or default_end
    existing = sorted(set(existing_dates or []))
    outside = [day for day in existing if day < start_date or day > end_date]
    try:
        window_dates, calls = fetch_tushare_trade_dates(start_date, end_date)
        years = range(int(start_date[:4]), int(end_date[:4]) + 1)
        year_counts = {
            str(year): sum(day.startswith(f"{year}-") for day in window_dates)
            for year in years
        }
        missing_years = [int(year) for year, count in year_counts.items() if not count]
        preserved_missing = [
            day
            for day in existing
            if start_date <= day <= end_date and int(day[:4]) in missing_years
        ]
        return CalendarFetchResult(
            dates=sorted(set(outside) | set(window_dates) | set(preserved_missing)),
            source="tushare.trade_cal",
            exchange=TUSHARE_EXCHANGE,
            window_start=start_date,
            window_end=end_date,
            tushare_calls=calls,
            preserved_outside_window=len(outside),
            year_counts=year_counts,
            missing_years=missing_years,
            preserved_missing_year_dates=len(preserved_missing),
        )
    except TushareCalendarError as exc:
        # 降级时合并全部现有行，避免一次上游故障删除已知的未来交易日。
        logger.warning("%s；降级 AkShare/Sina", exc)
        fallback_dates = fetch_sina_trade_dates()
        year_counts = {
            str(year): sum(day.startswith(f"{year}-") for day in fallback_dates)
            for year in range(int(start_date[:4]), int(end_date[:4]) + 1)
        }
        return CalendarFetchResult(
            dates=sorted(set(existing) | set(fallback_dates)),
            source="akshare.sina_fallback",
            exchange=TUSHARE_EXCHANGE,
            window_start=start_date,
            window_end=end_date,
            tushare_calls=exc.calls,
            preserved_outside_window=len(outside),
            fallback_reason=str(exc)[:500],
            year_counts=year_counts,
            missing_years=[
                int(year) for year, count in year_counts.items() if not count
            ],
        )


def fetch_trade_dates() -> list[str]:
    """兼容旧调用：返回滚动主窗口交易日列表。"""
    return fetch_trade_calendar().dates
