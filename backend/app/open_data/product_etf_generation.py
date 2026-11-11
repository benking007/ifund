"""Whole governed ETF denominator, resumable staging and one shared forward/reverse version."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import re
import time
import uuid

from app import db as database
from . import product_store as store, product_collectors as collectors, product_concepts as concepts
from .product_contracts import validate_query, validate_row
from .product_outcomes import NotDisclosedError
from .product_etf_schema import MAPPING_METHODS
from .product_etf_integrity import prove

KINDS = {'industry-etf-reverse': 142, 'concept-etf-reverse': 143}
METHOD = 'complete-governed-etf-shared-forward-reverse-v2-bound-immutable'
MAX_FACTS = 3000000
MAX_BYTES = 768000000
# This module only supplies fixed SQL. Table names and columns are not public input.
# pylint: disable=protected-access


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':'))


def scope_version(row):
    """Only a changed member needs re-collection; additions do not invalidate other checkpoints."""
    return hashlib.sha256(_encode(row).encode()).hexdigest()


def _directory_body(body):
    if (body.get('source') != 'fin_data_etf_directory' or body.get('product') != 'listed_etfs'
            or body.get('complete') is not True):
        raise ValueError('An official complete governed ETF directory is required')
    rows = body.get('items')
    if (not isinstance(rows, list) or not 1 <= len(rows) <= 5000 or body.get('listed_count') != len(rows)
            or not re.fullmatch(r'[0-9a-f]{32}', str(body.get('source_snapshot', '')))):
        raise ValueError('ETF denominator count/source version is not evidenced')
    symbols, codes = [], set()
    for row in rows:
        symbol = row.get('symbol')
        if (not isinstance(symbol, str) or not re.fullmatch(r'[0-9]{6}\.(?:SH|SZ)', symbol)
                or row.get('fund_code') != symbol[:6] or symbol[:6] in codes):
            raise ValueError('ETF directory has an ambiguous public identity')
        symbols.append(symbol)
        codes.add(symbol[:6])
    expected = hashlib.sha256(_encode(sorted(rows, key=lambda row: row['symbol'])).encode()).hexdigest()
    if body.get('directory_version') != expected:
        raise ValueError('ETF cohort fingerprint mismatch')
    observed = body.get('observed_at')
    if type(observed) not in (int, float) or not math.isfinite(observed) or observed > time.time()+60:
        raise ValueError('ETF directory observation time is invalid')
    return sorted(rows, key=lambda row: row['fund_code'])


def sync_directory(_args):
    """An offline fixed source call; discovery and requests subsequently read local SQL only."""
    body = concepts._source('etf_directory')
    rows = _directory_body(body)
    now, version = time.time(), uuid.uuid4().hex
    encoded = _encode(rows)
    with database.get_db().transaction() as conn:
        store._sql(conn, 'INSERT INTO open_fund_etf_directory VALUES(?,?,?,?,?,?,?,?)',
                   (version, body['directory_version'], body['source_snapshot'], body['observed_at'], now,
                    len(rows), encoded, hashlib.sha256(encoded.encode()).hexdigest()))
    day = datetime.fromtimestamp(body['observed_at'], timezone.utc).date().isoformat()
    return {'directory_version': version, 'quality': {'status': 'ready', 'complete': True, 'row_count': len(rows),
            'verified_empty': False, 'observed_at': body['observed_at'],
            'coverage_start': day, 'coverage_end': day, 'source': 'official-etf-basic-governed-full-directory',
            'method_version': 'complete-etf-denominator-v1'}}


def _directory(conn):
    snapshots = store._sql(conn, 'SELECT * FROM open_fund_etf_directory '
                           'ORDER BY recorded_at DESC,version_id DESC LIMIT 1', fetch=True)
    if not snapshots:
        raise LookupError('Complete ETF denominator has not been synchronized')
    snapshot = snapshots[0]
    raw = snapshot['payload_json'].encode()
    if len(raw) > 8000000 or hashlib.sha256(raw).hexdigest() != snapshot['payload_hash']:
        raise LookupError('Newest ETF directory corrupted')
    rows = json.loads(raw)
    if not isinstance(rows, list) or len(rows) != snapshot['row_count']:
        raise LookupError('ETF denominator physical row count differs')
    _directory_body({'source': 'fin_data_etf_directory', 'product': 'listed_etfs', 'complete': True,
                     'items': rows, 'listed_count': snapshot['row_count'], 'directory_version': snapshot['cohort_hash'],
                     'source_snapshot': snapshot['source_snapshot'], 'observed_at': snapshot['observed_at']})
    return snapshot, rows


def members():
    """Expose every real ETF, including unresolved local scopes as explicit work, not silent exclusions."""
    with database.get_db().transaction(read_only=True) as conn:
        snapshot, rows = _directory(conn)
    return {'complete': True, 'universe': 'verified_etf_funds', 'total': len(rows), 'included_count': len(rows),
            'excluded_count': 0, 'directory_version': snapshot['cohort_hash'],
            'source': 'official-full-etf-basic-listed-governed-v1',
            'members': [{'scope_id': row['fund_code'], 'fund_code': row['fund_code'], 'ts_code': row['symbol'],
                         'scope_version': scope_version(row)}
                        for row in rows]}


def directory_member(fund_code):
    """Exact ETF-domain identity; generic fund_basic coarse/O aliases cannot override ETF authority."""
    if not isinstance(fund_code, str) or not re.fullmatch(r'[0-9]{6}', fund_code):
        raise ValueError('An exact public ETF code is required')
    with database.get_db().transaction(read_only=True) as conn:
        snapshot, rows = _directory(conn)
        selected = [row for row in rows if row['fund_code'] == fund_code]
        if len(selected) != 1:
            raise ValueError('Fund is absent from the complete governed listed ETF denominator')
        local = store._sql(conn, "SELECT * FROM fund_basic_ext WHERE fund_code=? AND source='tushare'",
                           (fund_code,), fetch=True)
    row = selected[0]
    return {**(local[0] if local else {}), 'fund_code': fund_code, 'ts_code': row['symbol'],
            'name': row.get('name'), 'market': 'E', 'source': 'tushare:etf_basic',
            'directory_version': snapshot['cohort_hash'], 'scope_version': scope_version(row),
            **({'found_date': row['found_date']} if row.get('found_date') else {})}


def _copy_scope(conn, generation, product, code, job):
    if job['method_version'] != MAPPING_METHODS[product]:
        raise ValueError('An unreviewed method cannot enter the ETF shared version')
    store._verify_batch_storage(conn, product, [job], [job['batch_id']])
    facts = store._sql(conn, 'SELECT payload_json,etf_flag FROM open_fund_product_fact WHERE batch_id=?',
                       (job['batch_id'],), fetch=True)
    association = 'industryCode' if product == 142 else 'conceptCode'
    measure = 'holdingAssetAnnual' if product == 142 else 'holdingWeightAnnual'
    records, unique, size = [], set(), 0
    for fact in facts:
        row = validate_row(product, json.loads(fact['payload_json']))
        key, weight = row.get(association), row.get(measure)
        identity_ok = (row.get('fundCode') == code and fact['etf_flag'] == 1
                       and isinstance(key, str) and key not in unique)
        measure_ok = type(weight) in (int, float) and math.isfinite(weight) and 0 <= weight <= 100
        if not identity_ok or not measure_ok:
            raise ValueError('ETF staged scope contains an invalid identity/measure')
        unique.add(key)
        encoded = _encode(row)
        size += len(encoded.encode())
        records.append((generation, code, key, weight, encoded, hashlib.sha256(encoded.encode()).hexdigest()))
    concepts._write_many(conn, 'INSERT INTO open_fund_etf_generation_fact VALUES(?,?,?,?,?,?)', records)
    return len(records), size


def _plan_sources(conn, product, rows):
    counts = {'eligible': 0, 'not_disclosed': 0, 'not_applicable': 0}
    evidence, plans = [], []
    for row in rows:
        code = row['fund_code']
        jobs = store._sql(conn, 'SELECT * FROM open_fund_product_batch WHERE product_id=? '
                          "AND scope_kind='fund' AND scope_key=? AND status='ready' "
                          'ORDER BY recorded_at DESC,batch_id DESC LIMIT 1', (product, code), fetch=True)
        proof = store.disclosure_state(conn, product, code, after=jobs[0]['recorded_at'] if jobs else 0)
        job = None
        if proof:
            recorded = json.loads(proof['evidence_json'])
            if recorded.get('scope_version') != scope_version(row):
                raise LookupError('ETF eligibility evidence belongs to another directory version')
            if proof['status'] == 'not_applicable':
                declared = recorded.get('required_report_period')
                if (not row.get('found_date') or recorded.get('found_date') != row['found_date']
                        or not isinstance(declared, str) or row['found_date'] <= declared):
                    raise LookupError('ETF non-applicability contradicts the official setup date')
            status, source_version, scope_rows = proof['status'], proof['version_id'], 0
        else:
            if not jobs:
                raise LookupError('ETF directory member lacks a complete report or eligibility proof')
            job = jobs[0]
            metadata = json.loads(job.get('scope_metadata_json') or '{}')
            if metadata.get('scope_version') != scope_version(row):
                raise LookupError('ETF staged report belongs to another denominator version')
            status, source_version, scope_rows = 'eligible', job['batch_id'], job['row_count']
        counts[status] += 1
        evidence.append((code, source_version, status, scope_rows))
        plans.append((code, source_version, status, scope_rows, job))
    return counts, evidence, plans


def materialize(args):
    """Atomic version-vector cutoff; unchanged inputs reuse facts instead of rewriting millions of rows."""
    product, generation, now = KINDS[args.kind], uuid.uuid4().hex, time.time()
    counts = {'eligible': 0, 'not_disclosed': 0, 'not_applicable': 0}
    evidence, plans = [], []
    with database.get_db().transaction(consistent_snapshot=True) as conn:
        prove(conn, store._sql)
        directory, rows = _directory(conn)
        counts, evidence, plans = _plan_sources(conn, product, rows)
        fingerprint = collectors.evidence(evidence)
        existing = store._sql(conn, 'SELECT * FROM open_fund_etf_generation WHERE product_id=? '
                              'ORDER BY observed_at DESC,generation_id DESC LIMIT 1', (product,), fetch=True)
        reuse = (existing and existing[0]['method_version'] == METHOD
                 and existing[0]['cohort_hash'] == directory['cohort_hash']
                 and existing[0]['evidence_hash'] == fingerprint)
        if reuse:
            version = existing[0]
            actual = store._sql(conn, 'SELECT COUNT(*) AS n FROM open_fund_etf_generation_fact '
                                'WHERE generation_id=?', (version['generation_id'],), fetch=True)[0]['n']
            recorded = store._sql(conn, 'SELECT fund_code,source_version,eligibility_status,row_count '
                                  'FROM open_fund_etf_generation_scope WHERE generation_id=?',
                                  (version['generation_id'],), fetch=True)
            digest = collectors.evidence(sorted((row['fund_code'], row['source_version'],
                                                row['eligibility_status'], row['row_count']) for row in recorded))
            reuse = (actual == version['fact_count'] and len(recorded) == len(rows)
                     and digest == fingerprint)
        if reuse:
            version = existing[0]
            generation, facts = version['generation_id'], version['fact_count']
            published = version['observed_at']
        else:
            facts, total_bytes, published = 0, 0, now
            for code, source_version, status, expected_count, job in plans:
                if job:
                    scope_rows, byte_count = _copy_scope(conn, generation, product, code, job)
                    if scope_rows != expected_count:
                        raise LookupError('An ETF scope changed during its publication cutoff')
                    facts += scope_rows
                    total_bytes += byte_count
                if facts > MAX_FACTS or total_bytes > MAX_BYTES:
                    raise ValueError('Complete ETF mapping generation exceeds configured bounded capacity')
                store._sql(conn, 'INSERT INTO open_fund_etf_generation_scope VALUES(?,?,?,?,?)',
                           (generation, code, source_version, status, expected_count))
            store._sql(conn, 'INSERT INTO open_fund_etf_generation VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (generation, product, directory['cohort_hash'], directory['version_id'], published,
                        len(rows), counts['eligible'], counts['not_disclosed'], counts['not_applicable'], facts,
                        total_bytes, METHOD, fingerprint))
    prune_versions(product)
    scope = {'catalog_fund_count': len(rows), 'eligible_fund_count': counts['eligible'],
             'undisclosed_fund_count': counts['not_disclosed'], 'not_applicable_fund_count': counts['not_applicable']}
    if not counts['eligible']:
        raise NotDisclosedError('no_etf_in_the_complete_directory_has_disclosed_the_required_report',
                                'complete-etf-directory-and-verified-report-eligibility', scope,
                                observed_at=published)
    day = datetime.fromtimestamp(published, timezone.utc).date().isoformat()
    return {'generation': generation, 'reused': bool(reuse), 'quality': {'status': 'ready', 'complete': True,
            'row_count': facts, 'verified_empty': facts == 0, 'observed_at': published,
            'coverage_start': day, 'coverage_end': day, 'source': 'full-governed-etf-reports',
            'method_version': METHOD, 'dataset_scope': scope}}


def prune_versions(product):
    """Keep two immutable published caches, deleting by indexed generation only, never one huge JSON value."""
    with database.get_db().transaction() as conn:
        older = store._sql(conn, 'SELECT generation_id FROM open_fund_etf_generation WHERE product_id=? '
                           'ORDER BY observed_at DESC,generation_id DESC LIMIT 4 OFFSET 2', (product,), fetch=True)
        for row in older:
            key = (row['generation_id'],)
            store._sql(conn, 'DELETE FROM open_fund_etf_generation_fact WHERE generation_id=?', key)
            store._sql(conn, 'DELETE FROM open_fund_etf_generation_scope WHERE generation_id=?', key)
            store._sql(conn, 'DELETE FROM open_fund_etf_generation WHERE generation_id=?', key)


def prune_staging(product, code):
    """Old source headers retain PDF lineage; only two ready fact versions per ETF are needed for staging."""
    with database.get_db().transaction() as conn:
        older = store._sql(conn, 'SELECT batch_id FROM open_fund_product_batch WHERE product_id=? '
                           "AND scope_kind='fund' AND scope_key=? AND status='ready' "
                           'ORDER BY recorded_at DESC,batch_id DESC LIMIT 4 OFFSET 2', (product, code), fetch=True)
        for row in older:
            store._sql(conn, 'DELETE FROM open_fund_product_fact WHERE batch_id=?', (row['batch_id'],))
            store._sql(conn, "UPDATE open_fund_product_batch SET status='archived' WHERE batch_id=?",
                       (row['batch_id'],))


def query(product, values):
    """Forward and reverse selectors are SQL filters over exactly one published immutable version."""
    validate_query(product, values)
    page, size = values.get('pageNum', 1), values.get('pageSize', 100)
    with database.get_db().transaction(read_only=True, consistent_snapshot=True) as conn:
        directory, rows = _directory(conn)
        versions = store._sql(conn, 'SELECT * FROM open_fund_etf_generation WHERE product_id=? '
                             'ORDER BY observed_at DESC,generation_id DESC LIMIT 1', (product,), fetch=True)
        if not versions:
            raise LookupError('A complete shared ETF mapping version has not been published')
        version = versions[0]
        if (version['method_version'] != METHOD or version['cohort_hash'] != directory['cohort_hash']
                or version['scope_count'] != len(rows) or not version['eligible_count']):
            raise LookupError('Newest whole-ETF mapping cohort changed or has no disclosed reports')
        generation = version['generation_id']
        scopes = store._sql(conn, 'SELECT fund_code,row_count,eligibility_status,source_version '
                           'FROM open_fund_etf_generation_scope '
                           'WHERE generation_id=?', (generation,), fetch=True)
        physical = store._sql(conn, 'SELECT COUNT(*) AS n FROM open_fund_etf_generation_fact WHERE generation_id=?',
                             (generation,), fetch=True)[0]['n']
        denominator_ok = ({row['fund_code'] for row in scopes} == {row['fund_code'] for row in rows}
                          and len(scopes) == len(rows)
                          and sum(row['row_count'] for row in scopes) == version['fact_count'])
        digest = collectors.evidence(sorted((row['fund_code'], row['source_version'], row['eligibility_status'],
                                              row['row_count']) for row in scopes))
        content_ok = (physical == version['fact_count'] and version['payload_bytes'] <= MAX_BYTES
                      and digest == version['evidence_hash'])
        if not denominator_ok or not content_ok:
            raise LookupError('ETF published denominator or facts are physically incomplete')
        selected = values.get('fundCodes')
        if selected:
            by_code = {row['fund_code']: row for row in scopes}
            if any(code not in by_code or by_code[code]['eligibility_status'] != 'eligible' for code in selected):
                raise LookupError('A requested ETF has not disclosed its required full report')
        other = values.get('industryCodes') if product == 142 else values.get('conceptCodes')
        where, params = ['generation_id=?'], [generation]
        for column, targets in (('fund_code', selected), ('association_code', other)):
            if targets:
                where.append(column+' IN ('+','.join('?' for _ in targets)+')')
                params.extend(targets)
        if values.get('minAnnualWeight') is not None:
            where.append('annual_weight>=?')
            params.append(values['minAnnualWeight'])
        clause = ' AND '.join(where)
        total = store._sql(conn, 'SELECT COUNT(*) AS n FROM open_fund_etf_generation_fact WHERE '+clause,
                           params, fetch=True)[0]['n']
        byte_length = ('LENGTH(CAST(payload_json AS BLOB))' if hasattr(conn, 'row_factory')
                       else 'LENGTH(payload_json)')
        page_bytes = store._sql(conn, 'SELECT COALESCE(SUM(bytes),0) AS n FROM (SELECT '+byte_length+
                               ' AS bytes FROM open_fund_etf_generation_fact WHERE '+clause+
                               ' ORDER BY association_code,fund_code LIMIT ? OFFSET ?) bounded_page',
                               (*params, size, (page-1)*size), fetch=True)[0]['n']
        if page_bytes > 2000000:
            raise LookupError('ETF response page exceeds the bounded byte budget')
        facts = store._sql(conn, 'SELECT payload_json,payload_hash FROM open_fund_etf_generation_fact WHERE '+clause+
                          ' ORDER BY association_code,fund_code LIMIT ? OFFSET ?',
                          (*params, size, (page-1)*size), fetch=True)
        result = []
        for fact in facts:
            raw = fact['payload_json'].encode()
            if len(raw) > 1000000 or hashlib.sha256(raw).hexdigest() != fact['payload_hash']:
                raise LookupError('ETF published fact content hash mismatch')
            result.append(validate_row(product, json.loads(raw)))
    return {'items': result, 'total': total, 'page': page, 'page_size': size, 'source': 'ifund_products',
            'generation': generation, 'directory_version': version['cohort_hash'],
            'method_version': [METHOD], 'coverage': 'complete-governed-etf-disclosed-direct-A-equity-method',
            'dataset_scope': {'catalog_fund_count': version['scope_count'],
            'eligible_fund_count': version['eligible_count'], 'undisclosed_fund_count': version['undisclosed_count'],
            'not_applicable_fund_count': version['not_applicable_count']}}
