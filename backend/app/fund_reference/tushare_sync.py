"""Tushare 基金参考数据拉取与纯字段映射。"""
from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable

from app.fund_nav.fetch import tushare_client

COMPANY_FIELDS = (
    "name,shortname,short_enname,province,city,address,phone,office,website,"
    "chairman,manager,reg_capital,setup_date,end_date,employees,main_business,"
    "org_code,credit_code"
)
BASIC_FIELDS = (
    "ts_code,name,management,custodian,fund_type,found_date,due_date,list_date,"
    "issue_date,delist_date,issue_amount,m_fee,c_fee,duration_year,p_value,"
    "min_amount,exp_return,benchmark,status,invest_type,type,trustee,"
    "purc_startdate,redm_startdate,market"
)
SHARE_FIELDS = "ts_code,trade_date,fd_share"

_COMPANY_SUFFIX_RE = re.compile(r"(基金管理)?(股份)?(有限责任公司|有限公司)$")


def date_value(value: object) -> str | None:
    """把 Tushare YYYYMMDD 规范化为 MySQL/SQLite 可接受的 ISO 日期。"""
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return dt.date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


def number_value(value: object) -> float | None:
    """把空值、横线和数字字符串转换为可持久化数值。"""
    if value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def integer_value(value: object) -> int | None:
    """把员工数等上游值转换为整数。"""
    parsed = number_value(value)
    return int(parsed) if parsed is not None else None


def company_key(value: object) -> str:
    """生成仅用于公司名称关联的保守规范键。"""
    text = re.sub(r"[\s·•・]", "", str(value or "").strip())
    return _COMPANY_SUFFIX_RE.sub("", text)


def map_company_row(raw: dict) -> dict:
    """Tushare fund_company 行 → additive fund_company 行。"""
    return {
        "name": str(raw.get("name") or "").strip(),
        "short_name": str(raw.get("shortname") or "").strip() or None,
        "short_en_name": str(raw.get("short_enname") or "").strip() or None,
        "province": str(raw.get("province") or "").strip() or None,
        "city": str(raw.get("city") or "").strip() or None,
        "address": str(raw.get("address") or "").strip() or None,
        "phone": str(raw.get("phone") or "").strip() or None,
        "office": str(raw.get("office") or "").strip() or None,
        "website": str(raw.get("website") or "").strip() or None,
        "chairman": str(raw.get("chairman") or "").strip() or None,
        "general_manager": str(raw.get("manager") or "").strip() or None,
        "registered_capital": number_value(raw.get("reg_capital")),
        "setup_date": date_value(raw.get("setup_date")),
        "end_date": date_value(raw.get("end_date")),
        "employees": integer_value(raw.get("employees")),
        "main_business": str(raw.get("main_business") or "").strip() or None,
        "org_code": str(raw.get("org_code") or "").strip() or None,
        "credit_code": str(raw.get("credit_code") or "").strip() or None,
        "source": "tushare",
    }


def build_company_lookup(company_rows: Iterable[dict]) -> dict[str, int]:
    """只保留唯一公司名称键，避免简称碰撞时误关联。"""
    candidates: dict[str, set[int]] = {}
    for row in company_rows:
        company_id = int(row["company_id"])
        for value in (row.get("name"), row.get("short_name")):
            key = company_key(value)
            if key:
                candidates.setdefault(key, set()).add(company_id)
    return {
        key: next(iter(ids)) for key, ids in candidates.items() if len(ids) == 1
    }


def map_basic_row(
    raw: dict,
    reverse_map: dict[str, str],
    company_lookup: dict[str, int],
) -> dict | None:
    """Tushare fund_basic 行 → additive fund_basic_ext 行。"""
    ts_code = str(raw.get("ts_code") or "").strip().upper()
    fund_code = reverse_map.get(ts_code)
    if not fund_code:
        return None
    management = str(raw.get("management") or "").strip()
    return {
        "fund_code": fund_code,
        "ts_code": ts_code,
        "company_id": company_lookup.get(company_key(management)),
        "name": str(raw.get("name") or "").strip() or None,
        "management": management or None,
        "custodian": str(raw.get("custodian") or "").strip() or None,
        "market": str(raw.get("market") or "").strip().upper()[:1] or None,
        "status": str(raw.get("status") or "").strip().upper()[:1] or None,
        "fund_type": str(raw.get("fund_type") or "").strip() or None,
        "invest_type": str(raw.get("invest_type") or "").strip() or None,
        "fund_category": str(raw.get("type") or "").strip() or None,
        "trustee": str(raw.get("trustee") or "").strip() or None,
        "found_date": date_value(raw.get("found_date")),
        "due_date": date_value(raw.get("due_date")),
        "list_date": date_value(raw.get("list_date")),
        "issue_date": date_value(raw.get("issue_date")),
        "delist_date": date_value(raw.get("delist_date")),
        "purchase_start_date": date_value(raw.get("purc_startdate")),
        "redemption_start_date": date_value(raw.get("redm_startdate")),
        "issue_amount": number_value(raw.get("issue_amount")),
        "management_fee": number_value(raw.get("m_fee")),
        "custodian_fee": number_value(raw.get("c_fee")),
        "duration_years": number_value(raw.get("duration_year")),
        "par_value": number_value(raw.get("p_value")),
        "minimum_amount": number_value(raw.get("min_amount")),
        "expected_return": number_value(raw.get("exp_return")),
        "benchmark": str(raw.get("benchmark") or "").strip() or None,
        "source": "tushare",
    }


def map_share_row(raw: dict, reverse_map: dict[str, str]) -> dict | None:
    """Tushare fund_share 行 → additive fund_share 行。"""
    ts_code = str(raw.get("ts_code") or "").strip().upper()
    fund_code = reverse_map.get(ts_code)
    trade_date = date_value(raw.get("trade_date"))
    share_value = number_value(raw.get("fd_share"))
    if not fund_code or not trade_date or share_value is None:
        return None
    return {
        "fund_code": fund_code,
        "ts_code": ts_code,
        "trade_date": trade_date,
        "share_type": "fund_total",
        "share_value": share_value,
        "share_unit": "10k_shares",
        "source_field": "fd_share",
        "source": "tushare",
    }


def fetch_companies() -> list[dict]:
    """单次获取完整 fund_company。"""
    return tushare_client.call("fund_company", {}, COMPANY_FIELDS)


def fetch_all_basic(*, page_size: int = 5000) -> list[dict]:
    """按市场/状态分片分页获取 fund_basic 全量并按 ts_code 去重。"""
    rows_by_ts_code: dict[str, dict] = {}
    for market in ("E", "O"):
        # 无 status 的切片用于覆盖上游状态为空或非 L/D/I 的少量旧基金。
        for status in ("L", "D", "I", ""):
            offset = 0
            while True:
                params: dict[str, object] = {
                    "market": market,
                    "offset": offset,
                    "limit": page_size,
                }
                if status:
                    params["status"] = status
                batch = tushare_client.call(
                    "fund_basic",
                    params,
                    BASIC_FIELDS,
                )
                for row in batch:
                    ts_code = str(row.get("ts_code") or "").strip().upper()
                    if ts_code:
                        rows_by_ts_code[ts_code] = row
                if len(batch) < page_size:
                    break
                offset += page_size
    return list(rows_by_ts_code.values())


def _page_share(params: dict[str, object], page_size: int) -> list[dict]:
    """按 offset 分页调用 fund_share，直到不足一页。"""
    rows: list[dict] = []
    offset = 0
    for _page in range(200):
        page = dict(params)
        page["limit"] = page_size
        page["offset"] = offset
        batch = tushare_client.call("fund_share", page, SHARE_FIELDS)
        rows.extend(batch)
        if len(batch) < page_size:
            return rows
        offset += page_size
    raise RuntimeError("fund_share 分页超过 200 页，拒绝推进水位")


def fetch_share_history(
    ts_code: str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    page_size: int = 2000,
) -> list[dict]:
    """按 offset 分页获取一只基金的份额历史。"""
    params: dict[str, object] = {"ts_code": ts_code}
    if start_date:
        params["start_date"] = tushare_client.to_yyyymmdd(start_date)
    if end_date:
        params["end_date"] = tushare_client.to_yyyymmdd(end_date)
    return _page_share(params, page_size)


def fetch_share_by_trade_date(
    trade_date: str,
    *,
    page_size: int = 2000,
) -> list[dict]:
    """按交易日拉取全市场基金份额，包含场内和场外代码。

    ``fund_share`` 的 market 过滤是交易所 SH/SZ，不能用来代表场外 ``.OF``。
    增量只传 trade_date，再由本地映射表丢弃无法对应的代码。
    """
    return _page_share(
        {"trade_date": tushare_client.to_yyyymmdd(trade_date)},
        page_size,
    )


def share_incremental_start(
    watermark: dt.date | None,
    latest_fact: dt.date | None,
) -> dt.date:
    """增量起点为水位或事实最大日的次日。两者都没有时拒绝全历史。"""
    anchor = watermark if watermark is not None else latest_fact
    if anchor is None:
        raise ValueError(
            "fund_share 没有增量水位，也没有历史行；拒绝从全历史开始。"
            "请先执行 scripts/sync_tushare_fund_share.py --all --resume"
        )
    return anchor + dt.timedelta(days=1)


def share_empty_day_settled(
    day: dt.date, end: dt.date, *, settle_days: int = 5
) -> bool:
    """空日只有离开发布延迟窗口后才允许推进水位。

    份额经常晚于自然日才出现在 Tushare。最近几天返回 0 行时不记水位，
    下次增量会重试；更早的空日（休市或确无披露）可以跳过。
    """
    if settle_days < 1:
        raise ValueError("settle_days 必须为正数")
    return day <= end - dt.timedelta(days=settle_days)


def iter_share_dates(start: dt.date, end: dt.date) -> list[dt.date]:
    """闭区间自然日。空区间返回空列表。"""
    if start > end:
        return []
    days: list[dt.date] = []
    cursor = start
    while cursor <= end:
        days.append(cursor)
        cursor += dt.timedelta(days=1)
    return days


def select_basic_samples(rows: list[dict], per_group: int = 5) -> list[dict]:
    """选择 ETF/LOF/场外/退市四组样本，去重后最多 4×per_group。"""
    predicates = (
        lambda row: "ETF" in str(row.get("name") or "").upper(),
        lambda row: "LOF" in str(row.get("name") or "").upper(),
        lambda row: str(row.get("market") or "").upper() == "O"
        and str(row.get("status") or "").upper() != "D",
        lambda row: str(row.get("status") or "").upper() == "D",
    )
    selected: list[dict] = []
    seen: set[str] = set()
    for predicate in predicates:
        count = 0
        for row in rows:
            ts_code = str(row.get("ts_code") or "").upper()
            if ts_code in seen or not predicate(row):
                continue
            selected.append(row)
            seen.add(ts_code)
            count += 1
            if count >= per_group:
                break
    return selected
