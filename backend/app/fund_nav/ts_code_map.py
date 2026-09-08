"""Tushare fund_basic → iFund 六位码映射构建与查询。"""

from __future__ import annotations

# The client imports this mapper lazily only when resolving a per-fund code.
import datetime as dt
import hashlib
import json
import re
import time
from collections import defaultdict

from app import db as database
from app.fund_nav.fetch import tushare_client

MAP_TABLE = "fund_ts_code_map"
QUAR_TABLE = "fund_ts_code_quarantine"
ALIAS_TABLE = "fund_ts_code_alias"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fund_ts_code_map (
    fund_code VARCHAR(10) NOT NULL PRIMARY KEY,
    ts_code VARCHAR(20) NOT NULL UNIQUE,
    market CHAR(1) NOT NULL,
    tushare_name VARCHAR(255),
    tushare_status VARCHAR(8),
    match_kind VARCHAR(32) NOT NULL DEFAULT 'prefix_exact',
    source VARCHAR(32) NOT NULL DEFAULT 'fund_basic',
    verified_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_fund_ts_code_map_market ON fund_ts_code_map (market);

CREATE TABLE IF NOT EXISTS fund_ts_code_quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_kind VARCHAR(32) NOT NULL,
    code VARCHAR(20) NOT NULL,
    reason VARCHAR(255) NOT NULL DEFAULT '',
    detail_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (entity_kind, code)
);

CREATE TABLE IF NOT EXISTS fund_ts_code_alias (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_code VARCHAR(10) NOT NULL,
    primary_ts_code VARCHAR(20) NOT NULL,
    alias_ts_code VARCHAR(20) NOT NULL,
    primary_channel VARCHAR(4) NOT NULL,
    alias_channel VARCHAR(4) NOT NULL,
    source VARCHAR(32) NOT NULL DEFAULT 'tushare.fund_basic',
    source_evidence TEXT,
    status VARCHAR(16) NOT NULL DEFAULT 'active',
    verified_at TEXT NOT NULL,
    UNIQUE (fund_code, alias_ts_code)
);
CREATE INDEX IF NOT EXISTS ix_fund_ts_code_alias_alias ON fund_ts_code_alias (alias_ts_code);
CREATE INDEX IF NOT EXISTS ix_fund_ts_code_alias_status ON fund_ts_code_alias (status);
"""

_PREFIX_RE = re.compile(r"^(\d{6})\.(OF|SH|SZ)$", re.IGNORECASE)


def ensure_schema() -> None:
    """幂等创建映射表与隔离表。"""
    database.init_db(_SCHEMA)


def _base_code(ts_code: str) -> str | None:
    match = _PREFIX_RE.match(str(ts_code or "").strip().upper())
    return match.group(1) if match else None


def _suffix(ts_code: str) -> str:
    match = _PREFIX_RE.match(str(ts_code or "").strip().upper())
    return match.group(2) if match else ""


def fetch_all_fund_basic(
    *, page_size: int = 5000, page_sleep: float = 3.0
) -> list[dict]:
    """分页拉取 fund_basic 全量（E + O，各 status 切片避免 15000 截断）。"""
    rows: list[dict] = []
    seen: set[str] = set()
    fields = "ts_code,name,market,status,fund_type,invest_type"
    for market in ("E", "O"):
        for status in ("L", "D", "I", ""):
            params: dict[str, object] = {"market": market}
            if status:
                params["status"] = status
            offset = 0
            while True:
                page_params = dict(params)
                page_params["offset"] = offset
                page_params["limit"] = page_size
                batch = tushare_client.call("fund_basic", page_params, fields)
                if not batch:
                    break
                for row in batch:
                    ts = str(row.get("ts_code") or "").strip().upper()
                    if ts and ts not in seen:
                        seen.add(ts)
                        rows.append(row)
                if len(batch) < page_size:
                    break
                offset += page_size
                if page_sleep > 0:
                    time.sleep(page_sleep)
    return rows


def _pick_ts_code(candidates: list[dict]) -> tuple[dict | None, str]:
    """同一六位码多 ts_code 时按 market/后缀规则择优。"""
    if not candidates:
        return None, "empty"
    if len(candidates) == 1:
        return candidates[0], "prefix_exact"

    # 场内优先保留 .SH/.SZ（market=E）
    exchange = [c for c in candidates if str(c.get("market") or "").upper() == "E"]
    if len(exchange) == 1:
        return exchange[0], "share_class"
    if len(exchange) > 1:
        live = [c for c in exchange if str(c.get("status") or "").upper() == "L"]
        chosen = min(live or exchange, key=lambda r: r.get("ts_code") or "")
        return chosen, "ambiguous_resolved"

    # 场外：优先存续 .OF，A 份额通常 ts_code 较短或 status=L
    otc = [c for c in candidates if str(c.get("market") or "").upper() == "O"]
    live = [c for c in otc if str(c.get("status") or "").upper() == "L"]
    pool = live or otc or candidates
    chosen = min(pool, key=lambda r: r.get("ts_code") or "")
    return chosen, "ambiguous_resolved"


def build_mapping_rows(
    fund_basic_rows: list[dict],
    ifund_codes: set[str],
) -> tuple[list[dict], list[dict]]:
    """构建映射行与 quarantine 行。"""
    by_prefix: dict[str, list[dict]] = defaultdict(list)
    orphans: list[dict] = []
    for row in fund_basic_rows:
        base = _base_code(str(row.get("ts_code") or ""))
        if not base:
            orphans.append(
                {
                    "entity_kind": "tushare_orphan",
                    "code": str(row.get("ts_code") or ""),
                    "reason": "invalid_ts_code_format",
                    "detail_json": str(row),
                    "created_at": dt.datetime.now()
                    .astimezone()
                    .isoformat(timespec="seconds"),
                }
            )
            continue
        by_prefix[base].append(row)

    map_rows: list[dict] = []
    quarantine: list[dict] = []
    matched_bases: set[str] = set()
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")

    for base, candidates in sorted(by_prefix.items()):
        if base not in ifund_codes:
            for cand in candidates:
                quarantine.append(
                    {
                        "entity_kind": "tushare_orphan",
                        "code": str(cand.get("ts_code") or ""),
                        "reason": "no_ifund_fund_code",
                        "detail_json": f"base={base}",
                        "created_at": now,
                    }
                )
            continue
        chosen, kind = _pick_ts_code(candidates)
        if chosen is None:
            continue
        if len(candidates) > 1:
            quarantine.append(
                {
                    "entity_kind": "ambiguous",
                    "code": base,
                    "reason": "multiple_ts_codes",
                    "detail_json": ",".join(
                        sorted(str(c.get("ts_code") or "") for c in candidates)
                    ),
                    "created_at": now,
                }
            )
        map_rows.append(
            {
                "fund_code": base,
                "ts_code": str(chosen.get("ts_code") or "").upper(),
                "market": str(chosen.get("market") or "").upper()[:1] or "O",
                "tushare_name": chosen.get("name"),
                "tushare_status": chosen.get("status"),
                "match_kind": kind,
                "source": "fund_basic",
                "verified_at": now,
            }
        )
        matched_bases.add(base)

    for code in sorted(ifund_codes - matched_bases):
        quarantine.append(
            {
                "entity_kind": "ifund_unmatched",
                "code": code,
                "reason": "no_tushare_fund_basic",
                "detail_json": None,
                "created_at": now,
            }
        )

    return map_rows, quarantine + orphans


def load_map() -> dict[str, str]:
    """fund_code → ts_code 内存缓存。"""
    ensure_schema()
    rows = database.select(
        MAP_TABLE, [("select", "fund_code,ts_code"), ("limit", 500_000)]
    )
    return {
        str(r.get("fund_code") or "").strip(): str(r.get("ts_code") or "")
        .strip()
        .upper()
        for r in rows
        if r.get("fund_code") and r.get("ts_code")
    }


def resolve_ts_code(fund_code: str, cache: dict[str, str] | None = None) -> str:
    """解析 ts_code：映射表优先，否则回退 .OF（兼容旧逻辑）。"""
    code = str(fund_code or "").strip().upper()
    if code.endswith((".OF", ".SH", ".SZ")):
        return code
    mapping = cache if cache is not None else load_map()
    return mapping.get(code) or tushare_client.to_ts_code(code)


def build_alias_rows(map_rows: list[dict], quarantine_rows: list[dict]) -> list[dict]:
    """由 fund_basic 歧义证据生成独立 alias 行，不改变主映射。"""
    primary_by_code = {
        str(row.get("fund_code") or "").strip(): row
        for row in map_rows
        if row.get("fund_code") and row.get("ts_code")
    }
    aliases: dict[tuple[str, str], dict] = {}
    for evidence in quarantine_rows:
        if (
            evidence.get("entity_kind") != "ambiguous"
            or evidence.get("reason") != "multiple_ts_codes"
        ):
            continue
        fund_code = str(evidence.get("code") or "").strip()
        primary_row = primary_by_code.get(fund_code)
        if primary_row is None:
            continue
        primary = str(primary_row.get("ts_code") or "").strip().upper()
        candidates = sorted(
            {
                value.strip().upper()
                for value in str(evidence.get("detail_json") or "").split(",")
                if _base_code(value.strip().upper()) == fund_code
            }
        )
        if primary not in candidates:
            continue
        observed_at = str(
            evidence.get("created_at")
            or primary_row.get("verified_at")
            or dt.datetime.now().astimezone().isoformat(timespec="seconds")
        )
        source_evidence = json.dumps(
            {
                "authority": "tushare.fund_basic",
                "candidates": candidates,
                "map_source": str(primary_row.get("source") or "fund_basic"),
                "reason": "multiple_ts_codes",
                "observed_at": observed_at,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        for alias in candidates:
            if alias == primary:
                continue
            aliases[(fund_code, alias)] = {
                "fund_code": fund_code,
                "primary_ts_code": primary,
                "alias_ts_code": alias,
                "primary_channel": _suffix(primary),
                "alias_channel": _suffix(alias),
                "source": "tushare.fund_basic",
                "source_evidence": source_evidence,
                "status": "active",
                "verified_at": observed_at,
            }
    return [aliases[key] for key in sorted(aliases)]


def persist_aliases(alias_rows: list[dict]) -> int:
    """幂等 upsert alias 行，业务键为 (fund_code, alias_ts_code)。"""
    ensure_schema()
    if alias_rows:
        database.batch_insert(ALIAS_TABLE, alias_rows)
    return len(alias_rows)


def sync_aliases_from_stored_evidence() -> dict[str, object]:
    """从现有主映射与 fund_basic 隔离证据重建 alias 表。"""
    ensure_schema()
    map_rows = database.select(
        MAP_TABLE, [("order", "fund_code.asc"), ("limit", 500_000)]
    )
    before_fingerprint = hashlib.sha256(
        "\n".join(
            f"{row.get('fund_code')}={row.get('ts_code')}" for row in map_rows
        ).encode("utf-8")
    ).hexdigest()
    quarantine_rows = database.select(
        QUAR_TABLE,
        [
            ("entity_kind", "eq.ambiguous"),
            ("reason", "eq.multiple_ts_codes"),
            ("limit", 500_000),
        ],
    )
    alias_rows = build_alias_rows(map_rows, quarantine_rows)
    persist_aliases(alias_rows)
    map_rows_after = database.select(
        MAP_TABLE, [("order", "fund_code.asc"), ("limit", 500_000)]
    )
    after_fingerprint = hashlib.sha256(
        "\n".join(
            f"{row.get('fund_code')}={row.get('ts_code')}" for row in map_rows_after
        ).encode("utf-8")
    ).hexdigest()
    suffix_pairs: dict[str, int] = {}
    for row in alias_rows:
        pair = f"{row['alias_channel']}/{row['primary_channel']}"
        suffix_pairs[pair] = suffix_pairs.get(pair, 0) + 1
    return {
        "evidence_conflicts": len(quarantine_rows),
        "aliases_built": len(alias_rows),
        "aliases_stored": database.count(ALIAS_TABLE),
        "channel_pairs": suffix_pairs,
        "source": "tushare.fund_basic via fund_ts_code_quarantine",
        "primary_map_rows_before": len(map_rows),
        "primary_map_rows_after": len(map_rows_after),
        "primary_map_fingerprint_before": before_fingerprint,
        "primary_map_fingerprint_after": after_fingerprint,
        "primary_map_mutated": before_fingerprint != after_fingerprint,
    }


def persist_mapping(
    map_rows: list[dict], quarantine_rows: list[dict]
) -> dict[str, int]:
    """幂等写入映射与隔离区。"""
    ensure_schema()
    if map_rows:
        database.batch_insert(MAP_TABLE, map_rows)
    if quarantine_rows:
        database.batch_insert(QUAR_TABLE, quarantine_rows)
    aliases = persist_aliases(build_alias_rows(map_rows, quarantine_rows))
    return {
        "mapped": len(map_rows),
        "quarantine": len(quarantine_rows),
        "aliases": aliases,
    }
