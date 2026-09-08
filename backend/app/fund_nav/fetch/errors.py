"""基金净值抓取的业务异常。"""

from __future__ import annotations

import requests

F10_404 = "f10_404_code_invalid"
F10_EMPTY = "f10_empty_no_nav"
HOWBUY_EMPTY = "howbuy_empty_no_nav"
HOWBUY_REDIRECT = "howbuy_redirect_code_invalid"
AKSHARE_EMPTY = "akshare_empty_no_nav"
AKSHARE_PARSE = "akshare_parse_no_nav"
RANK_SNAPSHOT_MISSING = "rank_snapshot_missing_no_nav"


class NoNavDataError(ValueError):
    """数据源明确表示基金不存在或没有适用的单位净值。"""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def is_no_nav_data_error(exc: BaseException) -> bool:
    """识别 404、明确空数据及 AkShare 的 JavaScript 业务解析失败。"""
    if isinstance(exc, NoNavDataError):
        return True
    if exc.__class__.__name__ == "JSParseException":
        return True
    if isinstance(exc, requests.HTTPError):
        return getattr(exc.response, "status_code", None) == 404
    return False


def reason_for(exc: BaseException) -> str:
    """把无净值异常归一化成可持久化原因。"""
    if isinstance(exc, NoNavDataError):
        return exc.reason
    if isinstance(exc, requests.HTTPError):
        return AKSHARE_PARSE
    return AKSHARE_PARSE
