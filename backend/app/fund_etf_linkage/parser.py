"""联接基金名称解析与目标 ETF 匹配（纯函数，不碰库）。"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

# 长词在前，避免「国联」抢「国联安」、「中银」抢「中银证券」。
DEFAULT_COMPANY_TOKENS: tuple[str, ...] = (
    "交银施罗德",
    "工银瑞信",
    "景顺长城",
    "华泰柏瑞",
    "前海开源",
    "申万菱信",
    "方正富邦",
    "西部利得",
    "民生加银",
    "国投瑞银",
    "创金合信",
    "中银证券",
    "国泰海通",
    "弘毅远方",
    "光大保德信",
    "农银汇理",
    "兴证全球",
    "中信保诚",
    "中信建投",
    "国海富兰克林",
    "浦银安盛",
    "国寿安保",
    "西藏东财",
    "东方财富",
    "国联安",
    "易方达",
    "汇添富",
    "华夏",
    "南方",
    "广发",
    "富国",
    "鹏华",
    "嘉实",
    "博时",
    "华安",
    "银华",
    "华宝",
    "招商",
    "工银",
    "天弘",
    "平安",
    "景顺",
    "万家",
    "大成",
    "永赢",
    "建信",
    "东财",
    "摩根",
    "兴业",
    "泰康",
    "海富通",
    "浦银",
    "融通",
    "中银",
    "华富",
    "国联",
    "鑫元",
    "鹏扬",
    "国寿",
    "中金",
    "兴银",
    "交银",
    "兴全",
    "长城",
    "汇安",
    "银河",
    "新华",
    "南华",
    "财通",
    "华泰",
    "中欧",
    "国泰",
    "东方",
    "东吴",
    "诺安",
    "长信",
    "长盛",
    "泰康",
    "华商",
    "安信",
    "宝盈",
    "金鹰",
    "诺德",
    "圆信永丰",
    "富安达",
    "富荣",
    "先锋",
    "信达澳亚",
    "中邮",
    "中海",
    "中航",
    "九泰",
    "同泰",
    "嘉合",
    "天治",
    "太平",
    "宏利",
    "德邦",
    "恒越",
    "恒生前海",
    "惠升",
    "朱雀",
    "格林",
    "汇丰晋信",
    "汇泉",
    "江信",
    "泉果",
    "泓德",
    "泰信",
    "浙商",
    "淳厚",
    "湘财",
    "益民",
    "红土创新",
    "红塔红土",
    "英大",
    "蜂巢",
    "长安",
    "上银",
    "博远",
    "博道",
    "华润元大",
    "华西",
    "华银",
    "华泰保兴",
    "中加",
    "中泰",
    "新沃",
    "金信",
    "金元顺安",
    "人保",
    "众盈",
    "东兴",
    "东海",
    "东方阿尔法",
    "摩根士丹利",
    "上投摩根",
    "国新国证",
    "国融",
    "国都",
    "兴证",
    "长江",
    "山证",
    "渤海汇金",
    "财通证券",
)

_LEGAL_SUFFIX = re.compile(
    r"(?:基金管理有限责任公司|基金管理股份有限公司|基金管理有限公司|"
    r"基金管理公司|基金有限公司|基金公司|股份有限责任公司|"
    r"股份有限公司|有限责任公司|有限公司)$"
)

_FEEDER_TAIL = re.compile(
    r"(?:发起式|发起)?"
    r"联接"
    r"(?:美元|人民币)?"
    r"(?:现汇|现钞)?"
    r"(?:\(\s*(?:QDII|LOF|后端)\s*\))?"
    r"(?:[ACBEYFI])?"
    r"(?:\(\s*后端\s*\))?"
    r"$"
)

# 联接名未带这些修饰时，排除同指数的风格变体 ETF
_STYLE_MARKERS = ("增强", "价值", "成长", "ESG", "红利", "低波", "等权", "现金流", "股")

_INDEX_PREFIXES = ("中证全指", "上证", "深证", "中证", "国证", "沪深")

_INDEX_REWRITE = (
    ("科创板50成份", "科创50"),
    ("科创板50", "科创50"),
)

_CANONICAL_COMPANY = {
    "工银瑞信": "工银",
    "景顺长城": "景顺",
    "交银施罗德": "交银",
    "浦银安盛": "浦银",
    "国寿安保": "国寿",
    "西藏东财": "东财",
    "东方财富": "东财",
    "中银国际": "中银证券",
    "兴证全球": "兴全",
    "农银汇理": "农银",
}


@dataclass(frozen=True)
class ParsedFeeder:
    """联接基金名称拆出的公司 / 指数关键词。"""

    company: str
    index_raw: str
    index_keys: tuple[str, ...]
    is_enhanced: bool
    name: str


@dataclass(frozen=True)
class LinkageHit:
    """一条联接 → ETF 匹配结果。"""

    etf_code: str
    etf_name: str
    matched_by: str
    confidence: str


def is_feeder_name(name: str) -> bool:
    """名称是否像 ETF 联接（含发起式、短名省略 ETF）。"""
    return "联接" in (name or "")


def is_etf_name(name: str) -> bool:
    """场内 ETF：含 ETF、不含联接。"""
    text = name or ""
    return "ETF" in text and "联接" not in text


def strip_legal_suffix(company: str) -> str:
    """基金公司全称去掉法人后缀。"""
    text = (company or "").strip()
    prev = None
    while text and text != prev:
        prev = text
        text = _LEGAL_SUFFIX.sub("", text)
    return text.strip()


def etf_company_suffix(name: str) -> str:
    """`{指数}ETF{公司}` 的公司后缀；垃圾后缀返回空。"""
    if not is_etf_name(name) or "ETF" not in name:
        return ""
    suffix = name.rsplit("ETF", 1)[-1].strip()
    if not suffix or len(suffix) > 10:
        return ""
    if re.fullmatch(r"[A-Za-z0-9).(-]+", suffix):
        return ""
    if re.search(r"\d", suffix):
        return ""
    return suffix


def collect_company_tokens(
    etf_names: list[str] | None = None,
    fund_companies: list[str] | None = None,
    extra: list[str] | None = None,
) -> list[str]:
    """运行时公司词表：默认种子 + ETF 后缀 + fund_company 简称。"""
    tokens: set[str] = set(DEFAULT_COMPANY_TOKENS)
    for name in etf_names or []:
        suffix = etf_company_suffix(name)
        if suffix:
            tokens.add(suffix)
    for company in fund_companies or []:
        short = strip_legal_suffix(company)
        if short and 2 <= len(short) <= 12:
            tokens.add(short)
    for item in extra or []:
        if item:
            tokens.add(item)
    return sorted(tokens, key=len, reverse=True)


def _index_keys(raw: str) -> tuple[str, ...]:
    keys: list[str] = []

    def add(value: str) -> None:
        text = value.strip()
        if text and text not in keys:
            keys.append(text)

    add(raw)
    rewritten = raw
    for old, new in _INDEX_REWRITE:
        if old in rewritten:
            rewritten = rewritten.replace(old, new)
            add(rewritten)
    for prefix in _INDEX_PREFIXES:
        if rewritten.startswith(prefix) and len(rewritten) - len(prefix) >= 3:
            add(rewritten[len(prefix) :])
            break
    return tuple(keys)


def parse_feeder_name(
    name: str,
    company_tokens: list[str] | tuple[str, ...] | None = None,
) -> ParsedFeeder:
    """去掉联接/份额/发起式后缀，抽出公司前缀与指数关键词。"""
    original = (name or "").strip()
    tokens = (
        list(company_tokens)
        if company_tokens is not None
        else list(DEFAULT_COMPANY_TOKENS)
    )
    tokens = sorted(set(tokens), key=len, reverse=True)

    work = _FEEDER_TAIL.sub("", original)
    if "联接" in work:
        work = work[: work.index("联接")]
    work = re.sub(r"ETF$", "", work).strip()
    is_enhanced = "增强" in original

    company = ""
    rest = work
    for token in tokens:
        if work.startswith(token):
            company = token
            rest = work[len(token) :]
            break
    rest = rest.replace("ETF", "").replace("增强", "").strip()
    rest = re.sub(r"指数$", "", rest).strip()
    return ParsedFeeder(
        company=company,
        index_raw=rest,
        index_keys=_index_keys(rest),
        is_enhanced=is_enhanced,
        name=original,
    )


def _canonical_company(name: str) -> str:
    text = (name or "").strip()
    return _CANONICAL_COMPANY.get(text, text)


def companies_match(left: str, right: str) -> bool:
    """公司词对齐：别名表 + 前缀包含（工银/工银瑞信）。"""
    if not left or not right:
        return False
    left_c = _canonical_company(left)
    right_c = _canonical_company(right)
    if left_c == right_c:
        return True
    return left_c.startswith(right_c) or right_c.startswith(left_c)


def _apply_style_filter(parsed: ParsedFeeder, rows: list[dict]) -> list[dict]:
    """联接名没有的风格词，从 ETF 候选里去掉（避免 价值/成长 抢基础 ETF）。"""
    feeder = parsed.name
    kept = []
    for row in rows:
        etf_name = row.get("name") or ""
        extra = [
            mark for mark in _STYLE_MARKERS if mark in etf_name and mark not in feeder
        ]
        if extra:
            continue
        kept.append(row)
    return kept or rows


def match_feeder_to_etf(
    name: str,
    etfs: list[dict],
    company_tokens: list[str] | tuple[str, ...] | None = None,
    benchmark: str = "",
    invest_target: str = "",
    fund_company: str = "",
) -> LinkageHit | None:
    """按公司 + 指数关键词匹配场内 ETF；benchmark 可上调置信度。"""
    parsed = parse_feeder_name(name, company_tokens=company_tokens)
    if not parsed.company and fund_company:
        parsed = replace(parsed, company=strip_legal_suffix(fund_company))

    universe = [row for row in etfs if is_etf_name(row.get("name") or "")]
    hits: list[dict] = []
    used_key = ""
    for key in sorted(parsed.index_keys, key=len, reverse=True):
        if len(key) < 2:
            continue
        found = [row for row in universe if key in (row.get("name") or "")]
        found = _apply_style_filter(parsed, found)
        if found:
            hits = found
            used_key = key
            break
    if not hits:
        return None

    company_hits = [
        row
        for row in hits
        if companies_match(parsed.company, etf_company_suffix(row.get("name") or ""))
    ]
    if len(company_hits) > 1 and used_key:
        exact = [
            row
            for row in company_hits
            if (row.get("name") or "").rsplit("ETF", 1)[0] == used_key
        ]
        if exact:
            company_hits = exact

    cross_text = f"{benchmark or ''}{invest_target or ''}"
    bench_hit = any(key in cross_text for key in parsed.index_keys if len(key) >= 2)

    chosen: dict | None = None
    matched_by = ""
    confidence = ""

    if len(company_hits) == 1:
        chosen = company_hits[0]
        matched_by = "name_company_index"
        confidence = "high"
    elif len(company_hits) > 1:
        # 同名多代码（如 恒生ETF华夏 159920/513660）：取代码较小者，置信度降一档
        company_hits = sorted(company_hits, key=lambda row: str(row.get("code") or ""))
        chosen = company_hits[0]
        matched_by = "name_company_index"
        confidence = "medium"
    elif len(hits) == 1:
        chosen = hits[0]
        if bench_hit:
            matched_by = "benchmark_cross"
            confidence = "high"
        else:
            matched_by = "name_index_only"
            confidence = "medium"
    else:
        return None

    if chosen is None:
        return None
    if bench_hit and matched_by == "name_index_only":
        matched_by = "benchmark_cross"
        confidence = "high"

    return LinkageHit(
        etf_code=str(chosen.get("code") or ""),
        etf_name=str(chosen.get("name") or ""),
        matched_by=matched_by,
        confidence=confidence,
    )
