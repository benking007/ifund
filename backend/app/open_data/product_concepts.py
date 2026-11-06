"""Current THS A-equity themes over actual complete issuer-disclosed portfolios.

This is an independent NAV-weight method. THS identifiers are not CLS IDs and
current membership never becomes historical point-in-time classification.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
import re
import time
from urllib.parse import urlparse
import uuid

from app import db as database
from . import product_store as store, product_collectors as collectors
from .product_contracts import validate_query, validate_row
from .product_outcomes import NotDisclosedError, NotApplicableError

METHOD = 'verified-disclosed-portfolio-current-ths-nav-weight-v2'
FIELDS = {'fundCode', 'conceptCode', 'conceptName', 'holdingWeightLatest', 'holdingWeightAnnual'}


def _source(product, **params):
    import requests  # pylint: disable=import-outside-toplevel
    base = os.getenv('FIN_DATA_OPEN_BASE_URL', 'http://fin-data:8002/api/open-data/v1').rstrip('/')
    token = os.getenv('FIN_DATA_OPEN_TOKEN', '')
    parsed = urlparse(base)
    allowed = set(os.getenv('FIN_DATA_OPEN_ALLOWED_HOSTS', 'fin-data,localhost,127.0.0.1').split(','))
    identity_ok = parsed.hostname in allowed and parsed.scheme in {'http', 'https'} and bool(token)
    if not identity_ok or any((parsed.username, parsed.password, parsed.query, parsed.fragment)):
        raise ValueError('Approved internal financial source and token required')
    if product not in {'concepts', 'concept_memberships', 'etf_roles', 'stock_industry',
                       'cls_memberships', 'etf_directory'}:
        raise ValueError('Unreviewed internal product path')
    path = ('/cls-market/memberships' if product == 'cls_memberships' else
            '/etf-directory' if product == 'etf_directory' else '/market-products/'+product)
    response = requests.get(base+path, params=params,
                            headers={'X-Fin-Data-Token': token}, timeout=(5, 45), allow_redirects=False)
    response.raise_for_status()
    if len(response.content) > 8000000:
        raise ValueError('Internal financial source exceeds byte bound')
    body = response.json()
    expected = (('cls_market_products', 'memberships') if product == 'cls_memberships' else
                ('fin_data_etf_directory', 'listed_etfs') if product == 'etf_directory' else
                ('open_market_products', product))
    if (body.get('source'), body.get('product')) != expected:
        raise ValueError('Wrong fixed financial source product')
    return body


def dictionary():
    """Read the complete current dictionary with stable real page totals."""
    result, total = {}, None
    for page in range(1, 21):
        body = _source('concepts', page=page, page_size=500)
        total_ok = (isinstance(body.get('total'), int) and not isinstance(body.get('total'), bool)
                    and 1 <= body['total'] <= 10000)
        if (body.get('page') != page or body.get('page_size') != 500
                or not isinstance(body.get('items'), list) or not total_ok):
            raise ValueError('Concept dictionary pagination changed')
        total = body['total'] if total is None else total
        if body['total'] != total:
            raise ValueError('Concept dictionary changed while paging')
        for row in body['items']:
            code, name = row.get('conceptCode'), row.get('conceptName')
            if not isinstance(code, str) or not code or code in result or not isinstance(name, str) or not name:
                raise ValueError('Incomplete/duplicate concept dictionary identity')
            result[code] = name
        if len(result) == total:
            return result
    raise ValueError('Complete concept dictionary page budget exceeded')


def exposure_rows(stocks, memberships, names, fund_code, *,
                  source_identity=('open_market_products', 'concept_memberships', 'THS')):
    """Each requested stock needs a fresh complete subject snapshot, including zero."""
    expected = {row['stock_code'] for row in stocks}
    covered, rows = set(), []
    for body in memberships:
        if ((body.get('source'), body.get('product'), body.get('conceptSource')) != source_identity
                or body.get('completeSubjectSnapshots') is not True
                or body.get('historicalPointInTime') is not False):
            raise ValueError('Unverified current THS classification protocol')
        requested, supplied = body.get('requestedSymbols'), body.get('coveredSymbols')
        if (not isinstance(requested, list) or not isinstance(supplied, list)
                or set(requested) != set(supplied) or len(requested) != len(set(requested))):
            raise ValueError('Concept membership subject coverage is incomplete')
        if any(not isinstance(symbol, str) or not re.fullmatch(r'[0-9]{6}\.(?:SH|SZ|BJ)', symbol)
               for symbol in supplied):
            raise ValueError('Exact governed mainland stock symbols required')
        current = {symbol.split('.')[0] for symbol in supplied}
        if covered & current or not current <= expected:
            raise ValueError('Repeated or unrequested membership identity')
        covered.update(current)
        snapshots = body.get('subjectSnapshots')
        if (not isinstance(snapshots, list) or {x.get('symbol') for x in snapshots} != set(supplied)
                or len(snapshots) != len(supplied) or any(not x.get('snapshotId')
                or not isinstance(x.get('rowCount'), int) or isinstance(x.get('rowCount'), bool)
                or x['rowCount'] < 0 for x in snapshots)):
            raise ValueError('Physical concept snapshot evidence missing')
        items = body.get('items')
        if not isinstance(items, list) or body.get('total') != len(items):
            raise ValueError('Truncated membership facts')
        for snapshot in snapshots:
            filtered = snapshot.get('conceptRowCount', snapshot['rowCount'])
            if not isinstance(filtered, int) or isinstance(filtered, bool) or not 0 <= filtered <= snapshot['rowCount']:
                raise ValueError('Filtered current-N membership count is invalid')
            count = sum(row.get('stockCode') == snapshot['symbol'].split('.')[0] for row in items)
            if count != filtered:
                raise ValueError('Membership facts differ from attested subject row count')
        rows.extend(items)
    if covered != expected:
        raise ValueError('Complete disclosed A-equity portfolio coverage is required')
    weights = {row['stock_code']: row['nav_ratio'] for row in stocks}
    if len(weights) != len(stocks) or any(not math.isfinite(x) or not 0 <= x <= 100 for x in weights.values()):
        raise ValueError('Portfolio must have unique finite NAV weights')
    totals, seen = {}, set()
    for row in rows:
        stock, code = row.get('stockCode'), row.get('conceptCode')
        if (stock not in weights or code not in names or (stock, code) in seen
                or row.get('conceptName') != names.get(code)):
            raise ValueError('Wrong/duplicate stock or concept membership')
        seen.add((stock, code))
        totals[code] = totals.get(code, 0)+weights[stock]
    return [{'record_key': fund_code+':'+code, 'data': {'fundCode': fund_code, 'conceptCode': code,
             'conceptName': names[code], 'holdingWeightLatest': round(value, 8),
             'holdingWeightAnnual': round(value, 8)}} for code, value in sorted(totals.items())]


def membership_lifetime(responses, ttl_seconds):
    """Clip downstream facts to each actual source lease, never extend a shorter TTL."""
    now, lifetimes = time.time(), [ttl_seconds, 86400]
    for body in responses:
        for snapshot in body['subjectSnapshots']:
            stamp = datetime.fromisoformat(snapshot['observedAt'].replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                raise ValueError('Source observation needs an explicit time zone')
            observed = stamp.timestamp()
            expiry = snapshot.get('valid_until', observed+48*3600)
            if (type(expiry) not in (int, float) or not math.isfinite(expiry) or not math.isfinite(observed)
                    or observed > now+60 or expiry <= observed):
                raise ValueError('Invalid current membership source lifetime')
            lifetimes.append(min(expiry, observed+48*3600)-now)
    lifetime = min(lifetimes)
    if lifetime <= 0:
        raise ValueError('Current membership source expired')
    return max(1, int(lifetime))


def collect_scope(args):
    """Build one full disclosed portfolio against complete current stock memberships."""
    # pylint: disable=import-outside-toplevel
    from .product_reports import parse_document, full_stock_rows, report_index, latest_period_versions
    from .product_identity import canonical_master, not_yet_reportable  # pylint: disable=import-outside-toplevel
    master = canonical_master(args.fund_code)
    proof = not_yet_reportable(master, args.end, semiannual=True)
    if proof:
        version = store.record_disclosure_state(263, args.fund_code, 'official-canonical-fund-inception', proof,
                                                ttl_seconds=min(args.ttl_seconds, 86400), status='not_applicable')
        raise NotApplicableError('fund_not_established_in_completed_reporting_period',
                                 'official-canonical-fund-inception',
                                 {**proof, 'eligibility_version': version['version_id']},
                                 observed_at=version['observed_at'], valid_until=version['valid_until'])
    reports = report_index(args.fund_code)
    candidates = [row for row in reports if re.search(r'20[0-9]{2}年(?:年度|中期|半年度)报告', row.get('TITLE', ''))]
    if not candidates:
        proof = {'fund_code': args.fund_code, 'source_rows': len(reports), 'index': reports,
                 'required_report': 'annual-or-semiannual-full-equity', 'sha256': collectors.evidence(reports),
                 'index_uri': 'https://api.fund.eastmoney.com/f10/JJGG/'}
        version = store.record_disclosure_state(263, args.fund_code, 'official-complete-periodic-report-index', proof,
                                                ttl_seconds=min(args.ttl_seconds, 86400))
        raise NotDisclosedError('full_portfolio_not_yet_disclosed', 'official-complete-periodic-report-index',
                                {'fund_code': args.fund_code, 'index_sha256': proof['sha256'],
                                 'source_rows': len(reports), 'eligibility_version': version['version_id']},
                                observed_at=version['observed_at'], valid_until=version['valid_until'])
    metadata = latest_period_versions(candidates, periodic=True, latest_only=True)[-1]
    from .product_report_cache import load_report  # pylint: disable=import-outside-toplevel
    report = load_report(metadata, args.fund_code, parser=parse_document, periodic=True)
    stocks, names = full_stock_rows(report), dictionary()
    memberships = [_source('concept_memberships', codes=','.join(row['stock_code'] for row in stocks[i:i+20]))
                   for i in range(0, len(stocks), 20)]
    records = exposure_rows(stocks, memberships, names, args.fund_code)
    is_etf = False
    if master.get('market') == 'E':
        role = _source('etf_roles', codes=master['ts_code'])
        identity_ok = role.get('coveredSymbols') == [master['ts_code']] and role.get('completeSubjectRoles') is True
        roles = role.get('roles')
        if (not identity_ok or not isinstance(roles, list) or len(roles) != 1
                or not isinstance(roles[0].get('isETF'), bool) or roles[0].get('symbol') != master['ts_code']):
            raise ValueError('Exact governed ETF/LOF/REIT role required')
        if roles[0].get('assetType') not in {'ETF', 'LOF', 'REIT'}:
            raise ValueError('Unreviewed exchange-fund asset type')
        is_etf = role['roles'][0]['isETF']
        if is_etf != (roles[0]['assetType'] == 'ETF'):
            raise ValueError('ETF role flag conflicts with governed asset type')
    elif master.get('market') != 'O':
        raise ValueError('An actual exchange/nonexchange fund role is required')
    lifetime = membership_lifetime(memberships, args.ttl_seconds)
    for row in records:
        row['is_etf'] = is_etf
    return store.ingest(263, records, scope_kind='fund', scope_key=args.fund_code,
                        window_start=report['period'], window_end=report['period'],
                        source_name='issuer:full-portfolio+governed-current-ths', method_version=METHOD,
                        evidence_uri='https://api.fund.eastmoney.com/f10/JJGG/',
                        evidence_sha256=collectors.evidence({'pdf': report['hash'], 'memberships': memberships}),
                        complete_window=True, available_fields=FIELDS,
                        ttl_seconds=max(1, int(lifetime)))


def _write_many(conn, statement, records):
    if not records:
        return
    cursor = conn.cursor()
    try:
        cursor.executemany(statement if hasattr(conn, 'row_factory') else statement.replace('?', '%s'), records)
    finally:
        cursor.close()


def materialize(args):
    """Publish one atomic complete-catalog generation; incomplete catalog never replaces it."""
    names = dictionary()
    generation, count, now = uuid.uuid4().hex, 0, time.time()
    eligible, undisclosed, not_applicable = 0, 0, 0
    valid_until = now+min(args.ttl_seconds, 86400)
    with database.get_db().transaction() as conn:
        # pylint: disable=protected-access
        masters = store._sql(conn, "SELECT fund_code FROM fund_basic_ext WHERE status='L' AND source='tushare' "
                             "ORDER BY fund_code LIMIT 100001", fetch=True)
        if not masters or len(masters) > 100000:
            raise ValueError('Complete bounded active fund catalog required')
        for master in masters:
            jobs = store._sql(conn, "SELECT * FROM open_fund_product_batch WHERE product_id=263 "
                              "AND scope_kind='fund' AND scope_key=? AND status='ready' "
                              "ORDER BY recorded_at DESC,batch_id DESC LIMIT 1", (master['fund_code'],), fetch=True)
            proof = store.disclosure_state(conn, 263, master['fund_code'],
                                           after=jobs[0]['recorded_at'] if jobs else 0)
            if proof:
                valid_until = min(valid_until, proof['valid_until'])
                undisclosed += int(proof['status'] == 'not_disclosed')
                not_applicable += int(proof['status'] == 'not_applicable')
                store._sql(conn, 'INSERT INTO open_fund_concept_fund VALUES(?,?,?,?,?,?)',
                           (generation, master['fund_code'], proof['version_id'], '', 0, proof['status']))
                continue
            if (not jobs or jobs[0]['method_version'] != METHOD or jobs[0]['valid_until'] <= now):
                raise ValueError('A catalog member has no fresh complete concept exposure')
            eligible += 1
            job = jobs[0]
            valid_until = min(valid_until, job['valid_until'])
            store._verify_batch_storage(conn, 263, jobs, [job['batch_id']])
            facts = store._sql(conn, 'SELECT payload_json,etf_flag FROM open_fund_product_fact WHERE batch_id=?',
                               (job['batch_id'],), fetch=True)
            normalized = []
            for fact in facts:
                row = validate_row(263, json.loads(fact['payload_json']))
                identity_ok = row.get('fundCode') == master['fund_code']
                name_ok = names.get(row.get('conceptCode')) == row.get('conceptName')
                weight = row.get('holdingWeightLatest')
                weight_ok = weight is not None and math.isfinite(weight) and 0 <= weight <= 100
                if not identity_ok or not name_ok or fact['etf_flag'] not in (0, 1) or not weight_ok:
                    raise ValueError('Cached fund/concept/ETF identity mismatch')
                normalized.append((generation, row['fundCode'], row['conceptCode'], row['conceptName'],
                                   row['holdingWeightLatest'], job['window_end'], fact['etf_flag']))
            count += len(normalized)
            if count > 1000000:
                raise ValueError('Complete concept generation fact budget exceeded')
            _write_many(conn, 'INSERT INTO open_fund_concept_fact VALUES(?,?,?,?,?,?,?)', normalized)
            store._sql(conn, 'INSERT INTO open_fund_concept_fund VALUES(?,?,?,?,?,?)',
                       (generation, master['fund_code'], job['batch_id'], job['window_end'],
                        len(normalized), 'eligible'))
        _write_many(conn, 'INSERT INTO open_fund_concept_dictionary VALUES(?,?,?)',
                    [(generation, code, name) for code, name in names.items()])
        store._sql(conn, 'INSERT INTO open_fund_concept_generation VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (generation, now, valid_until, len(masters), count, len(names), METHOD,
                    eligible, undisclosed, not_applicable))
        # pylint: enable=protected-access
    status = 'ready' if eligible else ('not_disclosed' if undisclosed else 'not_applicable')
    return {'generation': generation, 'quality': {'status': status,
            'complete': True, 'row_count': count, 'verified_empty': count == 0 and eligible > 0,
            'evidence': {'eligible_funds': eligible, 'undisclosed_funds': undisclosed,
                         'not_applicable_funds': not_applicable, 'catalog_funds': len(masters)},
            'dataset_scope': {'eligible_fund_count': eligible, 'undisclosed_fund_count': undisclosed,
                              'not_applicable_fund_count': not_applicable, 'catalog_fund_count': len(masters)},
            'observed_at': now, 'valid_until': valid_until,
            'coverage_start': datetime.now(timezone.utc).date().isoformat(),
            'coverage_end': datetime.now(timezone.utc).date().isoformat(), 'source': 'issuer-full-portfolios+THS',
            'method_version': METHOD}}


def query(product, values):
    """Fixed indexed reads over one atomic generation, then real COUNT and pagination."""
    validate_query(product, values)
    codes = values.get('conceptCodes') or [values['conceptCode']]
    if product == 267 and values['matchType'] == 2:
        raise LookupError('Full quarterly concept portfolio not yet evidenced; top ten is insufficient')
    size = values.get('pageSize', 100) if product == 263 else 5000
    page = values.get('pageNum', 1)
    with database.get_db().transaction(read_only=True) as conn:
        # pylint: disable=protected-access
        generations = store._sql(conn, 'SELECT * FROM open_fund_concept_generation '
                                'ORDER BY observed_at DESC,generation_id DESC LIMIT 1', fetch=True)
        if not generations or generations[0]['valid_until'] <= time.time():
            raise LookupError('Latest complete concept generation missing or expired')
        meta = generations[0]
        generation = meta['generation_id']
        if not meta['eligible_count']:
            raise LookupError('No catalog fund has disclosed an eligible complete portfolio yet')
        actual = store._sql(conn, 'SELECT COUNT(*) AS total FROM open_fund_concept_fact WHERE generation_id=?',
                            (generation,), fetch=True)[0]['total']
        dictionary_count = store._sql(conn, 'SELECT COUNT(*) AS total FROM open_fund_concept_dictionary '
                                      'WHERE generation_id=?', (generation,), fetch=True)[0]['total']
        scopes = store._sql(conn, 'SELECT COUNT(*) AS total FROM open_fund_concept_fund WHERE generation_id=?',
                            (generation,), fetch=True)[0]['total']
        if (actual != meta['fact_count'] or dictionary_count != meta['dictionary_count']
                or scopes != meta['scope_count']):
            raise LookupError('Concept generation physical completeness failed')
        catalog_changed = store._sql(conn, "SELECT COUNT(*) AS total FROM fund_basic_ext m "
                                     "WHERE m.status='L' AND m.source='tushare' AND NOT EXISTS "
                                     '(SELECT 1 FROM open_fund_concept_fund f WHERE f.generation_id=? '
                                     'AND f.fund_code=m.fund_code)', (generation,), fetch=True)[0]['total']
        current_count = store._sql(conn, "SELECT COUNT(*) AS total FROM fund_basic_ext "
                                   "WHERE status='L' AND source='tushare'", fetch=True)[0]['total']
        if catalog_changed or current_count != meta['scope_count']:
            raise LookupError('Active fund catalog has new uncovered or removed members')
        known = store._sql(conn, 'SELECT concept_code FROM open_fund_concept_dictionary WHERE generation_id=? '
                          'AND concept_code IN ('+','.join('?' for _ in codes)+')', (generation, *codes), fetch=True)
        if {row['concept_code'] for row in known} != set(codes):
            raise ValueError('Unknown THS concept code; CLS identifiers are not aliases')
        where = ' WHERE generation_id=? AND concept_code IN ('+','.join('?' for _ in codes)+')'
        params = [generation, *codes]
        if product == 267:
            where += ' AND weight_nav>=?'
            params.append(values['threshold'])
            if values.get('isETF') is not None:
                where += ' AND is_etf=?'
                params.append(int(values['isETF']))
        total = store._sql(conn, 'SELECT COUNT(*) AS total FROM open_fund_concept_fact'+where,
                           params, fetch=True)[0]['total']
        rows = store._sql(conn, 'SELECT fund_code,concept_code,concept_name,weight_nav,report_period '
                          'FROM open_fund_concept_fact'+where+' ORDER BY fund_code,concept_code LIMIT ? OFFSET ?',
                          (*params, size, (page-1)*size), fetch=True)
        # pylint: enable=protected-access
    items = [{'fundCode': row['fund_code'], 'conceptCode': row['concept_code'], 'conceptName': row['concept_name'],
              'holdingWeightLatest': row['weight_nav'], 'holdingWeightAnnual': row['weight_nav'],
              **({'endDate': row['report_period']} if product == 267 else {})} for row in rows]
    return {'items': items, 'total': total, 'page': page, 'page_size': size, 'source': 'ifund_products',
            'method_version': [METHOD], 'coverage': 'complete_disclosed_eligible_subset_direct_Aequity_current_THS',
            'catalog_fund_count': meta['scope_count'], 'eligible_fund_count': meta['eligible_count'],
            'undisclosed_fund_count': meta['undisclosed_count'],
            'not_applicable_fund_count': meta['not_applicable_count']}
