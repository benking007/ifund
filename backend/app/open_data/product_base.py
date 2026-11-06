"""Standalone base producers for the read service; no legacy web startup required."""
from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import re
import time
import uuid

from app import db as database
from app.fund_reference import tushare_sync as reference
from . import product_collectors as collectors, product_store as store
from .product_contracts import day
from .product_identity import canonical_master
from .product_nav_types import NAV_UNIT_TYPES

KINDS = ('base-master', 'base-nav', 'base-dividends', 'base-holdings', 'base-returns', 'base-tenures', 'base-splits',
         'base-categories')



def _upsert(conn, table, keys, values):
    """Only internal constant table/column names; exact same transaction as watermark."""
    # pylint: disable=protected-access
    condition = ' AND '.join(key+'=?' for key in keys)
    params = tuple(values[key] for key in keys)
    if store._sql(conn, 'SELECT 1 FROM '+table+' WHERE '+condition+' LIMIT 1', params, fetch=True):
        update = {key: value for key, value in values.items() if key not in keys}
        if update:
            store._sql(conn, 'UPDATE '+table+' SET '+','.join(key+'=?' for key in update)+' WHERE '+condition,
                       (*update.values(), *params))
    else:
        store._sql(conn, 'INSERT INTO '+table+' ('+','.join(values)+') VALUES ('+
                   ','.join('?' for _ in values)+')', tuple(values.values()))
    # pylint: enable=protected-access


def _publish(conn, args, count, evidence, start, end, metadata=None):
    now = time.time()
    source = ('eastmoney:disclosed-fund-facts' if args.kind in {'base-holdings', 'base-tenures', 'base-splits'}
              else ('eastmoney:original-fund-classification' if args.kind == 'base-categories'
                    else 'tushare:official-fund-base'))
    method = args.kind+'-strict-v1'
    scope = args.fund_code or '*'
    if args.kind == 'base-holdings':
        scope += ':'+getattr(args, 'holding_type', 'stock')
    ttl = min(args.ttl_seconds, 86400)
    payload = (uuid.uuid4().hex, args.kind, scope, start, end, count, source, method,
               collectors.evidence(evidence), now, now+ttl, json.dumps(metadata or {}, ensure_ascii=False))
    # pylint: disable=protected-access
    store._sql(conn, 'INSERT INTO open_fund_base_watermark VALUES ('+','.join('?' for _ in payload)+')', payload)
    # Retain the current and previous observation per scope without unbounded growth.
    versions = store._sql(conn, 'SELECT version_id FROM open_fund_base_watermark WHERE kind=? AND scope_id=? '
                          'ORDER BY observed_at DESC,version_id DESC LIMIT 1000', (args.kind, scope), fetch=True)
    for version in versions[2:]:
        store._sql(conn, 'DELETE FROM open_fund_base_watermark WHERE version_id=?', (version['version_id'],))
    # pylint: enable=protected-access
    return {'status': 'ready', 'kind': args.kind, 'scope_id': scope, 'quality': {
        'status': 'ready', 'complete': True, 'row_count': count, 'verified_empty': count == 0,
        'source': source, 'method_version': method, 'coverage_start': start, 'coverage_end': end,
        'observed_at': now, 'valid_until': now+ttl, 'evidence_sha256': payload[8], **(metadata or {})}}


def _master(args):
    raw = []
    for market in ('E', 'O'):
        rows = collectors.collect_tushare('fund_basic', {'market': market}, reference.BASIC_FIELDS)
        if not rows or any(row.get('market') != market for row in rows):
            raise ValueError('A complete market fund directory is required')
        raw.extend(rows)
    companies = collectors.collect_tushare('fund_company', {}, reference.COMPANY_FIELDS)
    if not companies:
        raise ValueError('A complete manager-company directory is required')
    grouped, identities, unsupported, unknown_status = {}, set(), 0, 0
    for row in raw:
        ts_code = row.get('ts_code', '')
        if re.fullmatch(r'[0-9]{7,10}\.(?:OF|SH|SZ)', ts_code):
            # Old provider-only share-role identities do not fit the frozen
            # public six-digit namespace. Quarantine, never truncate them.
            unsupported += 1
            continue
        if (not re.fullmatch(r'[0-9]{6}\.(?:OF|SH|SZ)', ts_code) or ts_code in identities
                or not row.get('name')):
            raise ValueError('Unverified or repeated fund master identity/status: '+str((ts_code, row.get('status'))))
        identities.add(ts_code)
        if row.get('status') not in {'L', 'D', 'I'}:
            # Source status is optional. Keep the identity, exclude from the
            # active universe instead of inferring listing from its code/name.
            unknown_status += 1
            row = {**row, 'status': None}
        grouped.setdefault(ts_code[:6], []).append(row)
    now = datetime.now().isoformat(timespec='seconds')
    chosen, ambiguous = [], 0
    with database.get_db().transaction() as conn:
        # pylint: disable=protected-access
        existing_types = {row['code']: row['type'] for row in store._sql(conn,
                          'SELECT code,type FROM funds', fetch=True)}
        existing = {row['fund_code']: row['ts_code'] for row in store._sql(conn,
                    'SELECT fund_code,ts_code FROM fund_basic_ext', fetch=True)}
        for company in companies:
            value = reference.map_company_row(company)
            if not value['name']:
                raise ValueError('Unnamed management company')
            _upsert(conn, 'fund_company', ('name',), {**value, 'fetched_at': now})
        company_rows = store._sql(conn, 'SELECT company_id,name,short_name FROM fund_company', fetch=True)
        company_map = {}
        for row in company_rows:
            for label in (row['name'], row['short_name']):
                if label:
                    company_map.setdefault(label, set()).add(row['company_id'])
        for code, candidates in grouped.items():
            selected = [row for row in candidates if row['ts_code'] == existing.get(code)]
            if not selected:
                selected = candidates if len(candidates) == 1 else [row for row in candidates if row['market'] == 'E']
            if len(selected) != 1:
                ambiguous += 1
                continue
            row = selected[0]
            value = reference.map_basic_row(row, {row['ts_code']: code}, {})
            ids = company_map.get(row.get('management'), set())
            value['company_id'] = next(iter(ids)) if len(ids) == 1 else None
            _upsert(conn, 'fund_basic_ext', ('fund_code',), {**value, 'fetched_at': now})
            fund = {'code': code, 'name': row['name'], 'updated_at': now}
            if not existing_types.get(code):
                fund['type'] = row.get('fund_type') or ''
            _upsert(conn, 'funds', ('code',), fund)
            if row.get('fund_type'):
                _upsert(conn, 'fund_types', ('type_name',), {'type_name': row['fund_type']})
            chosen.append(code)
        # A removed/ambiguous role becomes unknown, not an invented delisting.
        for code in set(existing)-set(chosen):
            store._sql(conn, 'UPDATE fund_basic_ext SET status=NULL WHERE fund_code=?', (code,))
        # pylint: enable=protected-access
        today = date.today().isoformat()
        return _publish(conn, args, len(chosen), {'funds': raw, 'companies': companies}, today, today,
                        {'observed_provider_roles': len(raw), 'ambiguous_roles_excluded': ambiguous,
                         'company_count': len(companies), 'unsupported_code_roles_excluded': unsupported,
                         'unknown_status_roles_excluded_from_active': unknown_status})


def _categories(args):
    import akshare as ak  # pylint: disable=import-outside-toplevel,import-error
    frame = ak.fund_name_em()
    expected = {'基金代码', '基金简称', '基金类型'}
    if not expected <= set(frame.columns) or not 1 <= len(frame) <= 100000:
        raise ValueError('Complete original fund classification dictionary is required')
    raw = json.loads(frame.to_json(orient='records', force_ascii=False))
    seen, updated, unknown_categories = set(), 0, 0
    with database.get_db().transaction() as conn:
        # pylint: disable=protected-access
        canonical = {row['fund_code']: row['fund_type'] for row in store._sql(conn,
                     'SELECT fund_code,fund_type FROM fund_basic_ext', fetch=True)}
        # pylint: enable=protected-access
        enriched = set()
        for row in raw:
            code, category = row['基金代码'], row['基金类型']
            valid_code = isinstance(code, str) and re.fullmatch(r'[0-9]{6}', code) and code not in seen
            valid_category = isinstance(category, str) and 0 < len(category) <= 100
            if not valid_code:
                raise ValueError('Fund classification identity is invalid or repeated: '+str(code))
            seen.add(code)
            if not valid_category:
                unknown_categories += 1
                continue
            # Enrich only an exact governed fund. Do not create guessed roles
            # or infer ETF/FOF from the product's display name.
            if code in canonical:
                _upsert(conn, 'funds', ('code',), {'code': code, 'name': row['基金简称'], 'type': category})
                _upsert(conn, 'fund_types', ('type_name',), {'type_name': category})
                updated += 1
                enriched.add(code)
        # Missing/unknown current categories must not resurrect an older FOF
        # label. Retain only the independent current Tushare broad category.
        for code in set(canonical)-enriched:
            # pylint: disable=protected-access
            store._sql(conn, 'UPDATE funds SET type=? WHERE code=?', (canonical[code] or '', code))
            # pylint: enable=protected-access
        if not updated:
            raise ValueError('No exact canonical fund classification matches')
        today = date.today().isoformat()
        return _publish(conn, args, updated, raw, today, today,
                        {'provider_classification_rows': len(raw), 'canonical_exact_matches': updated,
                         'unknown_category_rows_excluded': unknown_categories,
                         'canonical_missing_or_unknown_category': len(canonical)-updated})


def _nav_rows(raw, masters, start, end):
    result, seen, excluded = [], set(), 0
    for row in raw:
        master = masters.get(row.get('ts_code'))
        when = day(row.get('nav_date'))
        if not start <= when <= end:
            raise ValueError('NAV provider ignored date range')
        if master is None or master.get('type') not in NAV_UNIT_TYPES:
            # Do not import income per ten thousand money-fund shares as unit NAV.
            excluded += 1
            continue
        identity = (master['fund_code'], when)
        if identity in seen:
            raise ValueError('Repeated NAV natural identity')
        seen.add(identity)
        values = {target: collectors.number(row.get(source)) for source, target in
                  (('unit_nav', 'nav'), ('accum_nav', 'acc_nav'), ('adj_nav', 'adj_nav'))}
        if values['nav'] is None or values['nav'] <= 0 or any(value is not None and value <= 0
                                                           for value in values.values()):
            raise ValueError('Invalid independent source NAV')
        result.append({'fund_code': identity[0], 'trade_date': when, **values,
                       'adj_src': 'tushare' if values['adj_nav'] is not None else None})
    return result, excluded


def _nav(args):
    end = day(args.end)
    lookback = getattr(args, 'lookback_days', 7)
    if args.start == '0001-01-01' and not args.fund_code:
        with database.get_db().transaction(read_only=True) as conn:
            # pylint: disable=protected-access
            prior = store._sql(conn, "SELECT 1 FROM open_fund_base_watermark WHERE kind='base-nav' LIMIT 1",
                               fetch=True)
            # pylint: enable=protected-access
        if not prior:
            lookback = max(lookback, getattr(args, 'bootstrap_days', 35))
    start = ((date.fromisoformat(end)-timedelta(days=lookback)).isoformat()
             if args.start == '0001-01-01' else day(args.start))
    if not 0 <= (date.fromisoformat(end)-date.fromisoformat(start)).days <= 366:
        raise ValueError('One NAV collection is limited to 366 calendar days; backfill in chunks')
    # pylint: disable=protected-access
    with database.get_db().transaction(read_only=True) as conn:
        masters = store._sql(conn, "SELECT e.fund_code,e.ts_code,f.type FROM fund_basic_ext e JOIN funds f "
                            "ON f.code=e.fund_code WHERE e.source='tushare'", fetch=True)
    # pylint: enable=protected-access
    by_ts = {row['ts_code']: row for row in masters}
    if not by_ts:
        raise ValueError('Collect the canonical fund directory before NAV')
    params = {'start_date': start.replace('-', ''), 'end_date': end.replace('-', '')}
    fields = 'ts_code,nav_date,unit_nav,accum_nav,adj_nav'
    if args.fund_code:
        master = canonical_master(args.fund_code)
        batches = [(None, {**params, 'ts_code': master['ts_code']})]
    else:
        current = date.fromisoformat(start)
        batches = []
        while current <= date.fromisoformat(end):
            batches.append((current.isoformat(), {'nav_date': current.strftime('%Y%m%d')}))
            current += timedelta(days=1)
    evidence, count, excluded = [], 0, 0
    for expected_day, query in batches:
        raw = collectors.collect_tushare('fund_nav', query, fields)
        if expected_day and any(day(row.get('nav_date')) != expected_day for row in raw):
            raise ValueError('NAV provider ignored exact date')
        if args.fund_code and any(row.get('ts_code') != master['ts_code'] for row in raw):
            raise ValueError('NAV fund identity mismatch')
        rows, skipped = _nav_rows(raw, by_ts, start, end)
        excluded += skipped
        count += len(rows)
        evidence.append({'date': expected_day, 'hash': collectors.evidence(raw), 'rows': len(raw)})
        # Network I/O never holds a DB transaction. One complete provider day
        # is atomically written; a later day failure never claims whole-job ready.
        with database.get_db().transaction() as conn:
            now = datetime.now().isoformat(timespec='seconds')
            for row in rows:
                _upsert(conn, 'fund_nav', ('fund_code', 'trade_date'), {**row, 'fetch_time': now})
    if args.fund_code and not count:
        raise ValueError('No independently verified unit NAV in requested window')
    with database.get_db().transaction() as conn:
        return _publish(conn, args, count, evidence, start, end, {'unverified_or_money_rows_excluded': excluded})


def _dividends(args):
    master = canonical_master(args.fund_code)
    raw = collectors.collect_tushare('fund_div', {'ts_code': master['ts_code']})
    start, end = day(args.start), day(args.end)
    prepared, revisions = [], {}
    for row in raw:
        if row.get('ts_code') != master['ts_code']:
            raise ValueError('Dividend source identity mismatch')
        if not row.get('ex_date'):
            continue  # Announced but not dated/implemented is not an ex-date fact.
        when, amount = day(row['ex_date']), collectors.number(row.get('div_cash'))
        if amount is None or amount < 0:
            raise ValueError('Invalid actual dividend per fund unit')
        if start <= when <= end:
            published = day(row['ann_date']) if row.get('ann_date') else ''
            previous = revisions.get(when)
            if previous and previous[0] == published and previous[1] != amount:
                raise ValueError('Conflicting same-publication dividend observations')
            if previous is None or previous[0] < published:
                revisions[when] = (published, amount)
    prepared = [{'fund_code': args.fund_code, 'ex_date': when, 'event_type': 'div',
                 'cash_per_unit': value[1], 'source': 'tushare'} for when, value in sorted(revisions.items())]
    with database.get_db().transaction() as conn:
        for row in prepared:
            _upsert(conn, 'fund_div_split', ('fund_code', 'ex_date', 'event_type'), row)
        return _publish(conn, args, len(prepared), raw, start, end)


def _holdings(args):
    import akshare as ak  # pylint: disable=import-outside-toplevel,import-error
    canonical_master(args.fund_code)
    year = int(args.end[:4])
    raw, prepared = {}, []
    kind = getattr(args, 'holding_type', 'stock')
    methods = {'stock': ak.fund_portfolio_hold_em, 'bond': ak.fund_portfolio_bond_hold_em}
    if kind not in methods:
        raise ValueError('Unknown holding type')
    for kind, function in ((kind, methods[kind]),):
        frame = function(symbol=args.fund_code, date=str(year))
        expected = {'季度', '占净值比例', '持仓市值', '股票代码' if kind == 'stock' else '债券代码'}
        if not expected <= set(frame.columns):
            raise ValueError('Holdings schema absent; exceptions are not verified empty portfolios')
        values = frame.to_dict('records')
        raw[kind] = json.loads(frame.to_json(orient='records', force_ascii=False))
        for row in values:
            period = re.fullmatch(r'(\d{4})年(\d)季度(?:股票|债券)投资明细', str(row['季度']))
            if not period or int(period[1]) != year or period[2] not in '1234':
                raise ValueError('Unknown holdings report-period label')
            code_field, name_field = ('股票代码', '股票名称') if kind == 'stock' else ('债券代码', '债券名称')
            code = str(row[code_field])
            if not re.fullmatch(r'[0-9A-Za-z.]{1,32}', code):
                raise ValueError('Invalid disclosed security identity')
            prepared.append({'fund_code': args.fund_code, 'quarter': period[1]+'Q'+period[2],
                             'holding_type': kind, 'asset_code': code, 'asset_name': str(row.get(name_field) or ''),
                             'hold_ratio': collectors.number(row.get('占净值比例')),
                             'hold_market_value': collectors.number(row.get('持仓市值')),
                             'hold_amount': collectors.number(row.get('持股数')) if kind == 'stock' else None})
    if not prepared:
        raise ValueError('Empty portfolio feed requires independent issuer zero-holdings evidence')
    identities = [(row['quarter'], row['holding_type'], row['asset_code']) for row in prepared]
    if len(set(identities)) != len(identities):
        raise ValueError('Duplicate disclosed holdings identity')
    with database.get_db().transaction() as conn:
        for row in prepared:
            _upsert(conn, 'fund_holdings', ('fund_code', 'quarter', 'holding_type', 'asset_code'), row)
        return _publish(conn, args, len(prepared), raw, str(year)+'-01-01', args.end)


def split_rows(document, fund_code):
    """Only a code-identified complete split table; literal no-split is verified empty."""
    import html  # pylint: disable=import-outside-toplevel
    from .product_eastmoney import _Tables  # pylint: disable=import-outside-toplevel,protected-access
    title = re.search(r'<title>(.*?)</title>', document, re.S | re.I)
    if not title or '('+fund_code+')' not in html.unescape(title[1]):
        raise ValueError('Split page subject identity mismatch')
    parser = _Tables()
    parser.feed(document)
    tables = [table for table in parser.tables if table and table[0] ==
              ['年份', '拆分折算日', '拆分类型', '拆分折算比例']]
    if len(tables) != 1:
        raise ValueError('Complete split table is required')
    if tables[0][1:] == [['暂无拆分信息!']]:
        return []
    result = []
    for row in tables[0][1:]:
        if len(row) != 4:
            raise ValueError('Split table columns changed')
        ratio = re.fullmatch(r'1[:：]([0-9]+(?:\.[0-9]+)?)', row[3])
        if not ratio or float(ratio[1]) <= 0:
            raise ValueError('Only explicit old-one to new-share split ratios are supported')
        result.append({'fund_code': fund_code, 'ex_date': day(row[1]), 'event_type': 'split',
                       'split_ratio': float(ratio[1]), 'source': 'eastmoney:explicit-split-table'})
    if len({row['ex_date'] for row in result}) != len(result):
        raise ValueError('Repeated split date')
    return result


def _splits(args):
    from curl_cffi import requests  # pylint: disable=import-outside-toplevel
    canonical_master(args.fund_code)
    response = requests.get('https://fundf10.eastmoney.com/fhsp_'+args.fund_code+'.html',
                            impersonate='chrome', timeout=30, allow_redirects=False)
    response.raise_for_status()
    if len(response.content) > 8000000:
        raise ValueError('Split table exceeds byte budget')
    raw = response.content.decode('utf-8')
    start, end = day(args.start), day(args.end)
    rows = [row for row in split_rows(raw, args.fund_code) if start <= row['ex_date'] <= end]
    with database.get_db().transaction() as conn:
        for row in rows:
            _upsert(conn, 'fund_div_split', ('fund_code', 'ex_date', 'event_type'), row)
        return _publish(conn, args, len(rows), raw, start, end)


def _tenures(args):
    from curl_cffi import requests  # pylint: disable=import-outside-toplevel
    from .product_manager_history import history_rows  # pylint: disable=import-outside-toplevel
    canonical_master(args.fund_code)
    response = requests.get('https://fundf10.eastmoney.com/jjjl_'+args.fund_code+'.html',
                            impersonate='chrome', timeout=30, allow_redirects=False)
    response.raise_for_status()
    if len(response.content) > 8000000:
        raise ValueError('Manager tenure response exceeds bound')
    raw = response.content.decode('utf-8')
    records = history_rows(raw, args.fund_code)
    with database.get_db().transaction() as conn:
        # pylint: disable=protected-access
        store._sql(conn, 'DELETE FROM fund_manager_tenure WHERE fund_code=?', (args.fund_code,))
        for seq, record in enumerate(records):
            value = record['data']
            _upsert(conn, 'fund_manager_tenure', ('fund_code', 'seq'), {
                'fund_code': args.fund_code, 'seq': seq, 'start_date': value['tenureStartDate'],
                'end_date': value.get('tenureEndDate'), 'is_current': value['isIncumbent'],
                'managers': value['fundManagerName'], 'tenure_return': value.get('returnTenure')})
        # pylint: enable=protected-access
        today = date.today().isoformat()
        return _publish(conn, args, len(records), raw, today, today)


def _returns(args):
    master = canonical_master(args.fund_code)
    # pylint: disable=protected-access
    with database.get_db().transaction(read_only=True) as conn:
        raw = store._sql(conn, 'SELECT trade_date,adj_nav FROM fund_nav WHERE fund_code=? '
                         "AND adj_src='tushare' AND adj_nav>0 ORDER BY trade_date LIMIT 100001",
                         (args.fund_code,), fetch=True)
    # pylint: enable=protected-access
    if not raw or len(raw) > 100000:
        raise ValueError('Verified adjusted NAV is required before interval return calculation')
    end = day(str(raw[-1]['trade_date']))
    latest = raw[-1]['adj_nav']
    last = date.fromisoformat(end)
    if not 0 <= (date.fromisoformat(day(args.end))-last).days <= 10:
        raise ValueError('Adjusted NAV observation is stale for the current return product')
    values = {}
    for label, months in (('1m', 1), ('3m', 3), ('6m', 6), ('1y', 12), ('3y', 36), ('5y', 60)):
        target_month = last.year*12+last.month-1-months
        import calendar  # pylint: disable=import-outside-toplevel
        year, month = divmod(target_month, 12)
        anchor = date(year, month+1, min(last.day, calendar.monthrange(year, month+1)[1])).isoformat()
        eligible = [row for row in raw if str(row['trade_date']) <= anchor]
        if eligible and (date.fromisoformat(anchor)-date.fromisoformat(str(eligible[-1]['trade_date']))).days <= 10:
            values['return_'+label] = (latest/eligible[-1]['adj_nav']-1)*100
    eligible = [row for row in raw if str(row['trade_date']) < str(last.year)+'-01-01']
    if eligible and (date(last.year, 1, 1)-date.fromisoformat(str(eligible[-1]['trade_date']))).days <= 10:
        values['return_ytd'] = (latest/eligible[-1]['adj_nav']-1)*100
    if not values:
        if master.get('found_date') and (last-date.fromisoformat(day(master['found_date']))).days < 28:
            from .product_outcomes import NotApplicableError  # pylint: disable=import-outside-toplevel
            raise NotApplicableError('fund_has_no_completed_calendar_return_interval',
                                     'canonical-inception-and-observed-adjusted-nav',
                                     {'fund_code': args.fund_code, 'found_date': day(master['found_date']),
                                      'first_observed_nav': str(raw[0]['trade_date']), 'end_date': end})
        raise ValueError('Stored window has no complete calendar interval anchor')
    with database.get_db().transaction() as conn:
        _upsert(conn, 'fund_details', ('fund_code',), {'fund_code': args.fund_code, 'trade_date': end, **values})
        return _publish(conn, args, 1, raw, str(raw[0]['trade_date']), end)


def collect(args):
    """Offline dispatch only; fixed providers, immutable evidence and atomic watermarks."""
    if args.kind not in KINDS:
        raise ValueError('Unreviewed base producer')
    return {'base-master': _master, 'base-nav': _nav, 'base-dividends': _dividends,
            'base-holdings': _holdings, 'base-tenures': _tenures, 'base-returns': _returns,
            'base-splits': _splits, 'base-categories': _categories}[args.kind](args)
