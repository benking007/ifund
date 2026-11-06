"""Auditable public fund facts with explicit migrations and verified windows.

The request path contains fixed SELECT statements only. Ingest is an offline
operator/worker operation; a failed batch never replaces a previous good one.
"""
from __future__ import annotations

from datetime import date
import hashlib
import json
import re
import sqlite3
import time
import uuid

from app import db as database
from .product_contracts import DATE_FIELDS, contracts, day, validate_query, validate_row

BASE_SCHEMA = """CREATE TABLE IF NOT EXISTS open_fund_base_watermark (
version_id VARCHAR(32) PRIMARY KEY,kind VARCHAR(32) NOT NULL,scope_id VARCHAR(32) NOT NULL,
window_start VARCHAR(10) NOT NULL,window_end VARCHAR(10) NOT NULL,row_count INTEGER NOT NULL,
source_name VARCHAR(128) NOT NULL,method_version VARCHAR(128) NOT NULL,evidence_sha256 VARCHAR(64) NOT NULL,
observed_at DOUBLE NOT NULL,valid_until DOUBLE NOT NULL,metadata_json LONGTEXT NOT NULL)"""

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS open_fund_product_batch (
    batch_id VARCHAR(32) PRIMARY KEY, product_id INTEGER NOT NULL,
    scope_kind VARCHAR(20) NOT NULL, scope_key VARCHAR(128) NOT NULL,
    window_start VARCHAR(10) NOT NULL, window_end VARCHAR(10) NOT NULL,
    source_name VARCHAR(128) NOT NULL, method_version VARCHAR(128) NOT NULL,
    evidence_sha256 VARCHAR(64) NOT NULL, evidence_uri VARCHAR(2048) NOT NULL,
    status VARCHAR(16) NOT NULL, coverage VARCHAR(16) NOT NULL,
    row_count INTEGER NOT NULL, recorded_at DOUBLE NOT NULL, valid_until DOUBLE NOT NULL,
    capabilities_json LONGTEXT NOT NULL,scope_metadata_json LONGTEXT)""",
    """CREATE TABLE IF NOT EXISTS open_fund_product_fact (
    batch_id VARCHAR(32) NOT NULL, product_id INTEGER NOT NULL,
    record_key VARCHAR(64) NOT NULL, fund_code VARCHAR(6), event_date VARCHAR(10),
    manager_name VARCHAR(128), industry_code VARCHAR(64), concept_code VARCHAR(64),
    benchmark_code VARCHAR(64), etf_flag INTEGER, payload_json LONGTEXT NOT NULL,
    PRIMARY KEY (batch_id,record_key))""",
)
DIRECTORY_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS open_fund_manager_directory_batch (
    snapshot_id VARCHAR(32) PRIMARY KEY, declared_count INTEGER NOT NULL,
    evidence_sha256 VARCHAR(64) NOT NULL, recorded_at DOUBLE NOT NULL, valid_until DOUBLE NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS open_fund_manager_directory (
    snapshot_id VARCHAR(32) NOT NULL, manager_id VARCHAR(32) NOT NULL, manager_name VARCHAR(128) NOT NULL,
    PRIMARY KEY(snapshot_id,manager_id))""",
)
CONCEPT_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS open_fund_concept_generation (
    generation_id VARCHAR(32) PRIMARY KEY,observed_at DOUBLE NOT NULL,valid_until DOUBLE NOT NULL,
    scope_count INTEGER NOT NULL,fact_count INTEGER NOT NULL,dictionary_count INTEGER NOT NULL,
    method_version VARCHAR(128) NOT NULL,eligible_count INTEGER NOT NULL,undisclosed_count INTEGER NOT NULL,
    not_applicable_count INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS open_fund_concept_fact (
    generation_id VARCHAR(32) NOT NULL,fund_code VARCHAR(6) NOT NULL,concept_code VARCHAR(64) NOT NULL,
    concept_name VARCHAR(256) NOT NULL,weight_nav DOUBLE NOT NULL,report_period VARCHAR(10) NOT NULL,
    is_etf INTEGER NOT NULL,PRIMARY KEY(generation_id,concept_code,fund_code))""",
    """CREATE TABLE IF NOT EXISTS open_fund_concept_fund (
    generation_id VARCHAR(32) NOT NULL,fund_code VARCHAR(6) NOT NULL,batch_id VARCHAR(32) NOT NULL,
    report_period VARCHAR(10) NOT NULL,row_count INTEGER NOT NULL,eligibility_status VARCHAR(20) NOT NULL,
    PRIMARY KEY(generation_id,fund_code))""",
    """CREATE TABLE IF NOT EXISTS open_fund_concept_dictionary (
    generation_id VARCHAR(32) NOT NULL,concept_code VARCHAR(64) NOT NULL,concept_name VARCHAR(256) NOT NULL,
    PRIMARY KEY(generation_id,concept_code))""",
)

ELIGIBILITY_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS open_fund_disclosure_eligibility (
    version_id VARCHAR(32) PRIMARY KEY,product_id INTEGER NOT NULL,fund_code VARCHAR(6) NOT NULL,
    status VARCHAR(20) NOT NULL,source_name VARCHAR(128) NOT NULL,evidence_json LONGTEXT NOT NULL,
    evidence_sha256 VARCHAR(64) NOT NULL,observed_at DOUBLE NOT NULL,valid_until DOUBLE NOT NULL)""",
)

INDEXES = {
    "ix_etf_mapping_reverse": ("open_fund_etf_generation_fact", "generation_id,association_code,fund_code"),
    "ix_etf_generation_latest": ("open_fund_etf_generation", "product_id,observed_at"),
    "ix_open_fund_batch_scope": ("open_fund_product_batch", "product_id,scope_kind,scope_key,status,recorded_at"),
    "ix_fund_eligibility": ("open_fund_disclosure_eligibility", "product_id,fund_code,observed_at"),
    "ix_open_fund_fact_filter": ("open_fund_product_fact", "product_id,fund_code,event_date"),
}


def _sql(conn, statement, params=(), *, fetch=False):
    cursor = conn.cursor()
    try:
        cursor.execute(statement if isinstance(conn, sqlite3.Connection) else statement.replace("?", "%s"), params)
        return [dict(row) for row in cursor.fetchall()] if fetch else cursor.rowcount
    finally:
        cursor.close()


def migrate():
    """Explicit CLI migration only: importing or querying never creates tables."""
    with database.get_db().transaction() as conn:
        _sql(conn, BASE_SCHEMA)
        from .product_etf_schema import SCHEMA as etf_schema  # pylint: disable=import-outside-toplevel
        for statement in etf_schema:
            _sql(conn, statement)
        for statement in (*SCHEMA, *DIRECTORY_SCHEMA, *CONCEPT_SCHEMA, *ELIGIBILITY_SCHEMA):
            _sql(conn, statement)
        additions = {'open_fund_product_batch': {'scope_metadata_json': 'LONGTEXT'},
                     'open_fund_concept_generation': {'eligible_count': 'INTEGER NOT NULL DEFAULT 0',
                                                      'undisclosed_count': 'INTEGER NOT NULL DEFAULT 0',
                                                      'not_applicable_count': 'INTEGER NOT NULL DEFAULT 0'},
                     'open_fund_concept_fund': {'eligibility_status': "VARCHAR(20) NOT NULL DEFAULT 'eligible'"}}
        for table, fields in additions.items():
            existing = ({row['name'] for row in _sql(conn, 'PRAGMA table_info('+table+')', fetch=True)}
                        if isinstance(conn, sqlite3.Connection) else
                        {row['COLUMN_NAME'] for row in _sql(conn, 'SELECT COLUMN_NAME FROM information_schema.columns '
                         'WHERE table_schema=DATABASE() AND table_name=?', (table,), fetch=True)})
            for field, definition in fields.items():
                if field not in existing:
                    _sql(conn, 'ALTER TABLE '+table+' ADD COLUMN '+field+' '+definition)
        for name, (table, columns) in INDEXES.items():
            if isinstance(conn, sqlite3.Connection):
                _sql(conn, f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({columns})")
            elif not _sql(conn, "SELECT 1 FROM information_schema.statistics WHERE table_schema=DATABASE() "
                          "AND table_name=? AND index_name=?", (table, name), fetch=True):
                _sql(conn, f"CREATE INDEX {name} ON {table} ({columns})")

        from .product_etf_integrity import install  # pylint: disable=import-outside-toplevel
        install(conn, _sql)

# Explicit provenance arguments are intentional audit boundaries, not free-form SQL.
# pylint: disable=too-many-arguments,too-many-locals
def ingest(product, records, *, scope_kind, scope_key, window_start, window_end,
           source_name, method_version, evidence_uri, evidence_sha256,
           complete_window, coverage="partial", ttl_seconds=86400, available_fields=None, scope_metadata=None):
    """Atomically publish an attested, complete source window with exact rows.

    records are {record_key: provider natural identity, data: frozen public
    fields, is_etf: optional actual boolean}. Public units must already have
    been normalized by a tested collector; no implicit financial conversion.
    """
    if product not in contracts() or scope_kind not in {"fund", "benchmark", "manager", "global"}:
        raise ValueError("Invalid product scope")
    if (complete_window is not True or coverage not in {"partial", "full"}
            or isinstance(ttl_seconds, bool) or not 0 < ttl_seconds <= 604800):
        raise ValueError("Only verified complete windows with bounded freshness may be published")
    if not source_name or not method_version or not evidence_uri or not re.fullmatch(r"[0-9a-f]{64}", evidence_sha256):
        raise ValueError("Source, method and evidence are required")
    if any(host in evidence_uri.lower() for host in ("invetbot", "investoday")):
        raise ValueError("The comparison vendor is not an authorized production source")
    if len(scope_key) > 128 or len(source_name) > 128 or len(method_version) > 128:
        raise ValueError("Source scope too long")
    start, end = day(window_start), day(window_end)
    if start > end or len(records) > 100000:
        raise ValueError("Invalid ingest window or row bound")
    scope_metadata = scope_metadata or {}
    scope_fields = {'catalog_fund_count', 'eligible_fund_count', 'undisclosed_fund_count', 'not_applicable_fund_count'}
    allowed = scope_fields | ({'directory_version', 'scope_version'} if product in {142, 143} else set())
    if (not isinstance(scope_metadata, dict) or set(scope_metadata)-allowed
            or any(not isinstance(value, int) or isinstance(value, bool) or value < 0
                   for key, value in scope_metadata.items() if key not in {'directory_version', 'scope_version'})):
        raise ValueError('Invalid verified public scope metadata')
    for key in {'directory_version', 'scope_version'} & set(scope_metadata):
        if not re.fullmatch(r'[0-9a-f]{64}', scope_metadata[key]):
            raise ValueError('Invalid complete ETF identity fingerprint')
    prepared, keys, payload_bytes = [], set(), 0
    capabilities = set(available_fields or ())
    specification = contracts()[product]["responseSchema"]["properties"]["data"]
    fields = set(specification.get("items", specification).get("properties", {}))
    if capabilities-fields:
        raise ValueError("Unknown capability fields")
    for record in records:
        row = validate_row(product, record["data"])
        capabilities.update(row)
        natural = record.get("record_key")
        if not isinstance(natural, str) or not natural or len(natural) > 2048:
            raise ValueError("Provider natural record identity is required")
        key = hashlib.sha256(json.dumps([scope_kind, scope_key, natural]).encode()).hexdigest()
        if key in keys:
            raise ValueError("Duplicate provider identity in batch")
        keys.add(key)
        fund = row.get("fundCode")
        benchmark = row.get("benchmarkIndexCode")
        wrong_fund = scope_kind == "fund" and fund != scope_key
        wrong_benchmark = scope_kind == "benchmark" and benchmark != scope_key
        wrong_manager = scope_kind == "manager" and row.get("fundManagerName") != scope_key
        if wrong_fund or wrong_benchmark or wrong_manager:
            raise ValueError("Fact does not belong to the attested scope")
        event = day(row[DATE_FIELDS[product]]) if product in DATE_FIELDS and row.get(DATE_FIELDS[product]) else None
        if event is not None and not start <= event <= end:
            raise ValueError("Fact lies outside its verified window")
        flag = record.get("is_etf")
        if flag is not None and not isinstance(flag, bool):
            raise ValueError("is_etf must be a verified boolean")
        payload = json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        payload_bytes += len(payload.encode())
        if len(payload.encode()) > 1000000 or payload_bytes > 32000000:
            raise ValueError("Fact payload too large")
        prepared.append((key, fund, event, row.get("fundManagerName"), row.get("industryCode"),
                         row.get("conceptCode"), benchmark, int(flag) if flag is not None else None, payload))
    if records and all(isinstance(record.get("is_etf"), bool) for record in records):
        capabilities.add("__is_etf__")
    now, batch = time.time(), uuid.uuid4().hex
    ttl = min(ttl_seconds, 30) if product == 226 else ttl_seconds
    with database.get_db().transaction() as conn:
        _sql(conn, "INSERT INTO open_fund_product_batch VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             (batch, product, scope_kind, scope_key, start, end, source_name, method_version,
              evidence_sha256, evidence_uri, "ready", coverage, len(prepared), now, now+ttl,
              json.dumps(sorted(capabilities)), json.dumps(scope_metadata)))
        for row in prepared:
            _sql(conn, "INSERT INTO open_fund_product_fact VALUES(?,?,?,?,?,?,?,?,?,?,?)", (batch, product, *row))
    return {"batch_id": batch, "rows": len(prepared), "product": product,
            "quality": {"status": "ready", "complete": True, "row_count": len(prepared),
                        "verified_empty": not prepared, "coverage_start": start, "coverage_end": end,
                        "source": source_name, "method_version": method_version, "observed_at": now,
                        "valid_until": now+ttl, "dataset_scope": scope_metadata}}
# pylint: enable=too-many-arguments,too-many-locals


def _scopes(query, product=None):
    if product == 251 and query.get("fundManagerName"):
        return "manager", [query["fundManagerName"]]
    if query.get("fundCode") or query.get("fundCodes"):
        return "fund", query.get("fundCodes") or [query["fundCode"]]
    if query.get("benchmarkIndexCode") or query.get("benchmarkIndexCodes"):
        return "benchmark", query.get("benchmarkIndexCodes") or [query["benchmarkIndexCode"]]
    return "global", ["*"]


def _cover(batches, start, end):
    cursor = date.fromisoformat(start).toordinal()
    for batch in sorted(batches, key=lambda value: value["window_start"]):
        left, right = (date.fromisoformat(batch[field]).toordinal() for field in ("window_start", "window_end"))
        if left > cursor:
            break
        cursor = max(cursor, right+1)
        if cursor > date.fromisoformat(end).toordinal():
            return True
    return False


def _payload_filters(conn, product, query):
    filters, params = ["revision_rank=1"], []

    def expression(field, *, numeric=False):
        value = "JSON_EXTRACT(payload_json,'$."+field+"')"
        if numeric:
            return "CAST("+value+" AS DECIMAL(36,12))"
        return value if isinstance(conn, sqlite3.Connection) else "JSON_UNQUOTE("+value+")"

    if product in {142, 143} and query.get('minAnnualWeight') is not None:
        field = 'holdingAssetAnnual' if product == 142 else 'holdingWeightAnnual'
        filters.append(expression(field, numeric=True)+'>=?')
        params.append(query['minAnnualWeight'])
    if query.get("title"):
        filters.append("LOWER("+expression("title")+") LIKE ? ESCAPE '!'")
        title = query["title"].lower().replace("!", "!!").replace("%", "!%").replace("_", "!_")
        params.append("%"+title+"%")
    for outer, field in (("announcementID", "announcementId"), ("employmentStatus", "employmentStatus"),
                         ("isIncumbent", "isIncumbent")):
        if query.get(outer) is not None:
            filters.append(expression(field, numeric=True)+"=?")
            params.append(query[outer])
    if product in {267, 268}:
        fields = {267: {1: "holdingWeightLatest", 2: "holdingWeightQuarterly", 3: "holdingWeightAnnual"},
                  268: dict(enumerate(("holdingWeightLatest", "holdingWeightQuarterly", "holdingWeightAnnual",
                                       "holdingRatioLatest", "holdingRatioQuarterly", "holdingRatioAnnual",
                                       "holdingRatioLatestPenetrated"), 1))}
        filters.append(expression(fields[product][query["matchType"]], numeric=True)+">=?")
        params.append(query["threshold"])
    return " WHERE "+" AND ".join(filters), params


def _coverage_shards(authority, start, end):
    """New complete windows supersede old intervals, including empty windows."""
    pending = [(date.fromisoformat(start).toordinal(), date.fromisoformat(end).toordinal())]
    selected = []
    for job in sorted(authority, key=lambda row: (row["recorded_at"], row["batch_id"]), reverse=True):
        left, right = (date.fromisoformat(job[field]).toordinal() for field in ("window_start", "window_end"))
        assigned, remaining = [], []
        for first, last in pending:
            low, high = max(first, left), min(last, right)
            if low > high:
                remaining.append((first, last))
                continue
            assigned.append((date.fromordinal(low).isoformat(), date.fromordinal(high).isoformat()))
            if first < low:
                remaining.append((first, low-1))
            if high < last:
                remaining.append((high+1, last))
        if assigned:
            selected.append({**job, "read_segments": assigned})
            pending = remaining
        if not pending:
            return selected
    raise LookupError("No complete source window covers the requested interval")


def _approved_jobs(jobs, scopes, start, end, *, strict_window=True):
    selected = []
    for scope in scopes:
        groups = {}
        for job in jobs:
            if job["scope_key"] == scope:
                groups.setdefault((job["source_name"], job["method_version"]), []).append(job)
        candidates = sorted(groups.values(), key=lambda group: max(job["recorded_at"] for job in group), reverse=True)
        authority = next((group for group in candidates if not strict_window or _cover(group, start, end)), None)
        if authority is None:
            raise LookupError("No single verified source covers this scope and window")
        chosen = (_coverage_shards(authority, start, end) if strict_window else
                  [max(authority, key=lambda row: (row["recorded_at"], row["batch_id"]))])
        # Expiry is checked AFTER choosing the authoritative version. An older
        # longer-lived value must never resurrect a superseded correction.
        if any(job["valid_until"] <= time.time() for job in chosen):
            raise LookupError("The latest covering source version has expired")
        selected.extend(chosen)
    return selected


def _segment_filter(jobs):
    clauses, values = [], []
    for job in jobs:
        for start, end in job.get("read_segments", []):
            clauses.append("(f.batch_id=? AND f.event_date>=? AND f.event_date<=?)")
            values.extend((job["batch_id"], start, end))
    return " OR ".join(clauses), values


def _required_fields(product, query):
    filter_fields = {"announcementID": "announcementId", "title": "title", "managerName": "fundManagerName",
                     "fundManagerName": "fundManagerName", "employmentStatus": "employmentStatus",
                     "isIncumbent": "isIncumbent", "isETF": "__is_etf__"}
    required = {value for key, value in filter_fields.items() if query.get(key) is not None}
    if product in {142, 143} and query.get('minAnnualWeight') is not None:
        required.add('holdingAssetAnnual' if product == 142 else 'holdingWeightAnnual')
    if (query.get("beginDate") or query.get("endDate")) and product in DATE_FIELDS:
        required.add(DATE_FIELDS[product])
    if product in {267, 268}:
        ordered = (("holdingWeightLatest", "holdingWeightQuarterly", "holdingWeightAnnual") if product == 267 else
                   ("holdingWeightLatest", "holdingWeightQuarterly", "holdingWeightAnnual", "holdingRatioLatest",
                    "holdingRatioQuarterly", "holdingRatioAnnual", "holdingRatioLatestPenetrated"))
        required.add(ordered[query["matchType"]-1])
    return required



def _verify_batch_storage(conn, product, jobs, ids):
    """Physical completeness and bytes are checked before parsing/pagination."""
    if sum(job["row_count"] for job in jobs) > 100000:
        raise LookupError("Selected physical fund snapshot rows exceed bounded query budget")
    byte_length = "LENGTH(CAST(payload_json AS BLOB))" if isinstance(conn, sqlite3.Connection) else (
        "LENGTH(payload_json)")
    counts = _sql(conn, "SELECT batch_id,COUNT(*) AS actual_count,SUM("+byte_length+") AS bytes "
                  "FROM open_fund_product_fact WHERE product_id=? AND batch_id IN ("+
                  ",".join("?" for _ in ids)+") GROUP BY batch_id", (product, *ids), fetch=True)
    actual = {row["batch_id"]: row["actual_count"] for row in counts}
    if (any(actual.get(job["batch_id"], 0) != job["row_count"] for job in jobs)
            or sum(row["bytes"] or 0 for row in counts) > 32000000):
        raise LookupError("Cached snapshot row count or byte budget failed verification")

def query_product(product, query):  # pylint: disable=too-many-locals
    """Fixed indexed read, real deduplicated total, no upstream side effects."""
    validate_query(product, query)
    kind, scopes = _scopes(query, product)
    # Without a historical lower bound, require an exhaustive-history batch.
    start = query.get("beginDate", "0001-01-01")
    end = query.get("endDate", date.today().isoformat())
    page, size = query.get("pageNum", 1), query.get("pageSize", 100)
    with database.get_db().transaction(read_only=True) as conn:
        global_purchase = False
        if product == 238:
            global_purchase = bool(_sql(conn, "SELECT 1 FROM open_fund_product_batch WHERE product_id=238 "
                                        "AND scope_kind='global' AND scope_key='*' LIMIT 1", fetch=True))
            if global_purchase:
                kind, scopes = 'global', ['*']
        jobs = _sql(conn, "SELECT * FROM open_fund_product_batch WHERE product_id=? AND scope_kind=? "
                    "AND scope_key IN ("+",".join("?" for _ in scopes)+") AND status='ready' "
                    "AND window_start<=? AND window_end>=? ORDER BY recorded_at DESC LIMIT 2000",
                    (product, kind, *scopes, end, start), fetch=True)
        if product == 251:
            _verify_manager_jobs(conn, query["fundManagerName"], jobs)
        required = _required_fields(product, query)
        strict_window = bool(query.get("beginDate") or query.get("endDate"))
        if product == 243:
            native = [job for job in jobs if job['source_name'] == 'issuer:fee-amendments'
                      or job['method_version'].startswith('em-disclosed-original-fee-tiers-v1')]
            if native:
                # Current fee tiers and dated amendment events are different
                # declared products. Do not let job completion order switch
                # an unfiltered current query into a historical-event list.
                jobs = [job for job in native if (job['source_name'] == 'issuer:fee-amendments') == strict_window]
        jobs = _approved_jobs(jobs, scopes, start, end, strict_window=strict_window)
        if any(not required <= set(json.loads(job["capabilities_json"])) for job in jobs):
            raise LookupError("The latest source version does not support the requested capability")
        ids = [job["batch_id"] for job in jobs]
        _verify_batch_storage(conn, product, jobs, ids)
        where, params = ["product_id=?", "batch_id IN ("+",".join("?" for _ in ids)+")"], [product, *ids]
        row_filters, row_params = [], []
        if global_purchase:
            requested = query.get('fundCodes') or [query['fundCode']]
            selected = _sql(conn, 'SELECT DISTINCT fund_code FROM open_fund_product_fact WHERE batch_id=? '
                            'AND fund_code IN ('+','.join('?' for _ in requested)+')',
                            (ids[0], *requested), fetch=True)
            if {row['fund_code'] for row in selected} != set(requested):
                raise LookupError('Requested fund is absent from the verified purchase-status universe')
            row_filters.append('fund_code IN ('+','.join('?' for _ in requested)+')')
            row_params.extend(requested)
        if product in DATE_FIELDS and strict_window:
            row_filters.extend(("event_date>=?", "event_date<=?"))
            row_params.extend((start, end))
        for single, multiple, column in (("industryCode", "industryCodes", "industry_code"),
                                          ("conceptCode", "conceptCodes", "concept_code")):
            values = query.get(multiple) or ([query[single]] if query.get(single) else [])
            if values:
                row_filters.append(column+" IN ("+",".join("?" for _ in values)+")")
                row_params.extend(values)
        if query.get("fundManagerName") or query.get("managerName"):
            name = query.get("fundManagerName") or query["managerName"]
            row_filters.append("manager_name LIKE ? ESCAPE '!'")
            row_params.append("%"+name.replace("!", "!!").replace("%", "!%").replace("_", "!_")+"%")
        if query.get("isETF") is not None:
            row_filters.append("etf_flag=?")
            row_params.append(int(query["isETF"]))
        # De-duplicate revisions and apply all filters inside SQL before count
        # and LIMIT. Do not repeatedly load/parse a long fund history in Flask.
        cte = ("WITH ranked AS (SELECT f.*,ROW_NUMBER() OVER(PARTITION BY f.record_key "
               "ORDER BY b.recorded_at DESC,f.batch_id DESC) AS revision_rank "
               "FROM open_fund_product_fact f JOIN open_fund_product_batch b ON b.batch_id=f.batch_id WHERE "+
               " AND ".join("f."+clause for clause in where))
        if strict_window and product in DATE_FIELDS:
            segments = _segment_filter(jobs)
            cte += " AND ("+segments[0]+")"
            params.extend(segments[1])
        cte += ") "
        restriction, filter_params = _payload_filters(conn, product, query)
        if row_filters:
            restriction += " AND "+" AND ".join(row_filters)
        total = _sql(conn, cte+"SELECT COUNT(*) AS total FROM ranked"+restriction,
                     (*params, *filter_params, *row_params), fetch=True)[0]["total"]
        facts = _sql(conn, cte+"SELECT payload_json FROM ranked"+restriction+
                     " ORDER BY event_date,fund_code,record_key LIMIT ? OFFSET ?",
                     (*params, *filter_params, *row_params, size, (page-1)*size), fetch=True)
        try:
            rows = [validate_row(product, json.loads(fact["payload_json"])) for fact in facts]
        except (ValueError, TypeError) as exc:
            raise LookupError("Persisted product fact failed validation") from exc
    return {"items": rows, "total": total, "page": page, "page_size": size,
            "source": "ifund_products", "method_version": sorted({job["method_version"] for job in jobs}),
            "coverage": "full" if all(job["coverage"] == "full" for job in jobs) else "partial",
            "valid_until": min(job["valid_until"] for job in jobs),
            "scope_metadata": [json.loads(job.get("scope_metadata_json") or "{}") for job in jobs],
            "window_start": min(job["window_start"] for job in jobs),
            "window_end": max(job["window_end"] for job in jobs),
            "verified_windows": sorted({(job["scope_key"], job["window_start"], job["window_end"]) for job in jobs})}



def publish_manager_directory(rows, declared_count, evidence_sha256, ttl_seconds=3600):
    """Atomically mirror a fully enumerated source-defined current manager directory."""
    if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool) or not 0 < ttl_seconds <= 604800:
        raise ValueError("Directory TTL must be a bounded integer")
    if (len(rows) != declared_count or not 1 <= declared_count <= 20000
            or len({row["id"] for row in rows}) != len(rows)
            or not re.fullmatch(r"[0-9a-f]{64}", evidence_sha256)):
        raise ValueError("Manager directory is incomplete or has duplicate identities")
    for row in rows:
        if (not re.fullmatch(r"[0-9]{6,12}", row["id"]) or not isinstance(row["name"], str)
                or not 1 <= len(row["name"].strip()) <= 128):
            raise ValueError("Unverified manager directory identity")
    snapshot, now = uuid.uuid4().hex, time.time()
    with database.get_db().transaction() as conn:
        _sql(conn, "INSERT INTO open_fund_manager_directory_batch VALUES(?,?,?,?,?)",
             (snapshot, declared_count, evidence_sha256, now, now+min(ttl_seconds, 3600)))
        for row in rows:
            _sql(conn, "INSERT INTO open_fund_manager_directory VALUES(?,?,?)", (snapshot, row["id"], row["name"]))
    today = date.today().isoformat()
    return {"snapshot_id": snapshot, "rows": declared_count,
            "quality": {"status": "ready", "complete": True, "row_count": declared_count,
                        "coverage_start": today, "coverage_end": today,
                        "source": "eastmoney:complete-current-manager-directory",
                        "method_version": "strict-id-sorted-directory-v1",
                        "observed_at": now, "valid_until": now+min(ttl_seconds, 3600)}}


def manager_identity(name, *, conn=None):
    """Only one exact name in a fresh, physically complete directory can resolve."""
    if conn is None:
        with database.get_db().transaction(read_only=True) as connection:
            return manager_identity(name, conn=connection)
    batches = _sql(conn, "SELECT * FROM open_fund_manager_directory_batch "
                   "ORDER BY recorded_at DESC,snapshot_id DESC LIMIT 1", fetch=True)
    if not batches or batches[0]["valid_until"] <= time.time():
        raise LookupError("The latest complete public manager directory must be fresh; no older fallback")
    batch = batches[0]
    rows = _sql(conn, "SELECT manager_id,manager_name FROM open_fund_manager_directory WHERE snapshot_id=?",
                (batch["snapshot_id"],), fetch=True)
    if len(rows) != batch["declared_count"] or len(rows) > 20000:
        raise LookupError("Manager directory physical completeness failed")
    matches = {row["manager_id"] for row in rows if row["manager_name"] == name}
    if len(matches) != 1:
        raise LookupError("Manager name does not identify exactly one current public-source manager")
    return next(iter(matches))



def _verify_manager_jobs(conn, name, jobs):
    identity = manager_identity(name, conn=conn)
    if any(job["source_name"] != "eastmoney:manager:"+identity for job in jobs):
        raise LookupError("Ambiguous or stale same-name manager source identities")


def prune_purchase_snapshots():
    """Offline current-status retention: keep the newest two, prune at most five old batches.

    This product has no historical event date. Retaining every four-minute
    full-universe revision would create millions of redundant facts daily.
    It never deletes the latest authority, including when that latest expires.
    """
    with database.get_db().transaction() as conn:
        rows = _sql(conn, "SELECT batch_id FROM open_fund_product_batch WHERE product_id=238 "
                    "AND scope_kind='global' AND scope_key='*' ORDER BY recorded_at DESC,batch_id DESC "
                    'LIMIT 5 OFFSET 2', fetch=True)
        for row in rows:
            _sql(conn, 'DELETE FROM open_fund_product_fact WHERE product_id=238 AND batch_id=?', (row['batch_id'],))
            _sql(conn, 'DELETE FROM open_fund_product_batch WHERE product_id=238 AND batch_id=?', (row['batch_id'],))
    return len(rows)


def record_disclosure_state(product, fund_code, source, evidence, *, ttl_seconds=86400, status='not_disclosed'):
    """Persist verified non-disclosure evidence separately from any financial facts."""
    if (product not in {142, 143, 263, 264} or not re.fullmatch(r'[0-9]{6}', fund_code) or not evidence
            or status not in {'not_disclosed', 'not_applicable'}):
        raise ValueError('Reviewed disclosure eligibility source required')
    raw = json.dumps(evidence, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))
    if len(raw.encode()) > 8000000 or not source or not 0 < ttl_seconds <= 86400:
        raise ValueError('Bounded source eligibility evidence required')
    version, now = uuid.uuid4().hex, time.time()
    with database.get_db().transaction() as conn:
        _sql(conn, 'INSERT INTO open_fund_disclosure_eligibility VALUES(?,?,?,?,?,?,?,?,?)',
             (version, product, fund_code, status, source, raw, hashlib.sha256(raw.encode()).hexdigest(),
              now, now+ttl_seconds))
    return {'version_id': version, 'observed_at': now, 'valid_until': now+ttl_seconds}


def disclosure_state(conn, product, fund_code, *, after=0):
    """Select the latest evidence before freshness checks; never revive an old eligibility."""
    rows = _sql(conn, 'SELECT * FROM open_fund_disclosure_eligibility WHERE product_id=? AND fund_code=? '
                'ORDER BY observed_at DESC,version_id DESC LIMIT 1', (product, fund_code), fetch=True)
    if not rows:
        return None
    row = rows[0]
    if row['observed_at'] <= after:
        return None
    encoded = row['evidence_json'].encode()
    if (row['status'] not in {'not_disclosed', 'not_applicable'} or row['valid_until'] <= time.time()
            or len(encoded) > 8000000
            or hashlib.sha256(encoded).hexdigest() != row['evidence_sha256']):
        raise LookupError('Latest disclosure eligibility evidence expired or corrupted')
    proof = json.loads(row['evidence_json'])
    if not proof or proof.get('fund_code') != fund_code:
        raise LookupError('Disclosure evidence subject mismatch')
    return row
