"""Actual ordered manager-team tenures, complete-catalog aggregation and no fake dates."""
from __future__ import annotations

from datetime import date
import html
import re

from app import db as database
from . import product_store as store, product_collectors as collectors, product_eastmoney as tables
from .product_contracts import day, validate_query
from .product_numbers import numeric

METHOD = 'exact-manager-team-historical-tenures-source-percentage-v2'
FIELDS = {'fundCode', 'fundManagerName', 'tenureStartDate', 'tenureEndDate', 'isIncumbent', 'returnTenure'}


def history_rows(document, fund_code):
    """Keep joint-management teams intact; their return is not individual manager alpha."""
    title = re.search(r'<title>(.*?)</title>', document, re.S | re.I)
    if not title or '('+fund_code+')' not in html.unescape(title[1]):
        raise ValueError('Manager-tenure fund identity mismatch')
    parser = tables._Tables()  # pylint: disable=protected-access
    parser.feed(document)
    candidates = [table for table in parser.tables if table and len(table[0]) == 5
                  and table[0][:4] == ['起始期', '截止期', '基金经理', '任职期间']
                  and table[0][4].startswith('任职回报')]
    if len(candidates) != 1 or not candidates[0][1:]:
        raise ValueError('Complete source tenure-list table is required')
    result, periods = [], set()
    for row in candidates[0][1:]:
        if len(row) != 5 or not row[2].strip():
            raise ValueError('Incomplete manager-team tenure row')
        start = day(row[0])
        end = None if row[1] == '至今' else day(row[1])
        if (end and end < start) or start > date.today().isoformat() or (start, end) in periods:
            raise ValueError('Invalid or repeated manager-team tenure dates')
        periods.add((start, end))
        value = {'fundCode': fund_code, 'fundManagerName': row[2].strip(), 'tenureStartDate': start,
                 'isIncumbent': 1 if end is None else 0}
        if end:
            value['tenureEndDate'] = end
        reported = numeric(row[4])
        if reported is not None:
            value['returnTenure'] = reported
        result.append({'record_key': fund_code+':'+start, 'data': value})
    if sum(row['data']['isIncumbent'] for row in result) != 1:
        raise ValueError('Exactly one explicitly current management team must be declared')
    return result


def collect_scope(args):
    """Offline fixed-provider fetch only; source dates remain tenure dates, not publication."""
    from curl_cffi import requests  # pylint: disable=import-outside-toplevel
    from .product_identity import canonical_master as _master  # pylint: disable=import-outside-toplevel
    _master(args.fund_code)
    uri = 'https://fundf10.eastmoney.com/jjjl_'+args.fund_code+'.html'
    response = requests.get(uri, impersonate='chrome', timeout=30, allow_redirects=False)
    response.raise_for_status()
    if len(response.content) > 8000000:
        raise ValueError('Manager-team source byte budget exceeded')
    document = response.content.decode('utf-8')
    today = date.today().isoformat()
    return store.ingest(252, history_rows(document, args.fund_code), scope_kind='fund', scope_key=args.fund_code,
                        window_start=today, window_end=today, source_name='eastmoney:manager-team-tenures',
                        method_version=METHOD, evidence_uri=uri, evidence_sha256=collectors.evidence(document),
                        complete_window=True, available_fields=FIELDS)


def _catalog(conn):
    # pylint: disable=protected-access
    rows = store._sql(conn, "SELECT fund_code FROM fund_basic_ext WHERE status='L' AND source='tushare' "
                      'ORDER BY fund_code LIMIT 100001', fetch=True)
    # pylint: enable=protected-access
    if not rows or len(rows) > 100000:
        raise LookupError('A complete bounded active fund catalog is required')
    return [row['fund_code'] for row in rows]


def materialize(_args):
    """Unknown or physically missing fund snapshots never produce a global empty list."""
    records, evidence = [], []
    with database.get_db().transaction(read_only=True) as conn:
        # pylint: disable=protected-access
        codes = _catalog(conn)
        for code in codes:
            jobs = store._sql(conn, "SELECT * FROM open_fund_product_batch WHERE product_id=252 "
                              "AND scope_kind='fund' AND scope_key=? AND status='ready' "
                              'ORDER BY recorded_at DESC,batch_id DESC LIMIT 1', (code,), fetch=True)
            if not jobs or jobs[0]['method_version'] != METHOD:
                raise LookupError('A catalog fund lacks complete current historical tenures')
            job = jobs[0]
            store._verify_batch_storage(conn, 252, jobs, [job['batch_id']])
            facts = store._sql(conn, 'SELECT payload_json FROM open_fund_product_fact WHERE batch_id=?',
                               (job['batch_id'],), fetch=True)
            import json  # pylint: disable=import-outside-toplevel
            for fact in facts:
                value = json.loads(fact['payload_json'])
                if value['fundCode'] != code:
                    raise LookupError('Historical-tenure snapshot identity corrupted')
                records.append({'record_key': code+':'+value['tenureStartDate'], 'data': value})
            if len(records) > 100000:
                raise LookupError('Historical-tenure population budget exceeded')
            evidence.append(job['evidence_sha256'])
        # pylint: enable=protected-access
    today = date.today().isoformat()
    return store.ingest(252, records, scope_kind='global', scope_key='*', window_start=today, window_end=today,
                        source_name='eastmoney:complete-catalog-manager-team-tenures',
                        method_version=METHOD+':'+collectors.evidence(codes)[:16],
                        evidence_uri='https://fundf10.eastmoney.com/', evidence_sha256=collectors.evidence(evidence),
                        complete_window=True, available_fields=FIELDS)


def query(values):
    """Verify catalog generation before applying exact source fields and real SQL pagination."""
    validate_query(252, values)
    with database.get_db().transaction(read_only=True) as conn:
        # pylint: disable=protected-access
        rows = store._sql(conn, "SELECT method_version FROM open_fund_product_batch WHERE product_id=252 "
                          "AND scope_kind='global' AND status='ready' ORDER BY recorded_at DESC,batch_id DESC "
                          'LIMIT 1', fetch=True)
        # pylint: enable=protected-access
        if not rows or rows[0]['method_version'] != METHOD+':'+collectors.evidence(_catalog(conn))[:16]:
            raise LookupError('Historical-tenure global catalog generation missing or changed')
    return store.query_product(252, values)
