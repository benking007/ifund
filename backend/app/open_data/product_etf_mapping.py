"""Disclosed ETF holdings associations, not thematic-index tracking assertions."""
from __future__ import annotations

from datetime import date
import re

from . import product_collectors as collectors, product_concepts as concepts, product_store as store
from .product_numbers import numeric
from .product_outcomes import NotApplicableError, NotDisclosedError

KINDS = {'industry-etf-mapping': 142, 'concept-etf-mapping': 143}
FIELDS = {142: {'publishDate', 'endDate', 'fundCode', 'fundName', 'industryCode', 'industryName', 'industryLevel',
                'holdingAssetAnnual', 'holdingNavAnnual'},
          143: {'publishDate', 'endDate', 'fundCode', 'fundName', 'conceptCode', 'conceptCategory', 'conceptName',
                'holdingWeightAnnual'}}


def gross_assets(report):
    """The explicitly total-asset composition sum, never the NAV balance."""
    values = []
    for table in report['tables']:
        header = ''.join(str(cell or '') for row in table[:4] for cell in row)
        if '基金总资产' not in header or '金额' not in header:
            continue
        for row in table:
            cells = [re.sub(r'\s+', '', str(cell or '')) for cell in row]
            if len(cells) == 4 and cells[1] == '合计':
                amount = numeric(cells[2])
                if amount and amount > 0:
                    values.append(amount)
    if len(values) != 1:
        raise ValueError('Exactly one explicit gross-asset composition CNY sum is required')
    if not re.search(r'金额单位[：:]?人民币元', re.sub(r'\s+', '', report['text'])):
        raise ValueError('Gross-asset currency unit is unverified')
    return values[0]


def complete_domestic_stocks(report):
    """Borderless issuer ALL-stock table with explicit ordinal/strategy boundaries.

    Rank resets are accepted only at separately titled index/active investment
    subsections. The same stock may belong to both; amounts and NAV weights
    are added, never silently deduplicated. Top-ten tables are not eligible.
    """
    text = report['text']
    compact = re.sub(r'\s+', '', text)
    if re.search(r'本基金(?:本)?报告期末未持有股票(?:投资)?[。；;]', compact):
        return _disclosed_stock_rows(report)
    anchor = re.search(r'^\s*(\d+(?:\.\d+)+)\s+期末按公允价值[^\n]*所有股票投资明细\s*$', text, re.M)
    if not anchor:
        return _disclosed_stock_rows(report)
    level = len(anchor[1].split('.'))
    tail = text[anchor.end():]
    end = next((match.start() for match in re.finditer(r'^\s*(\d+(?:\.\d+)+)\s+[^\n]+', tail, re.M)
                if len(match[1].split('.')) <= level), None)
    if end is None:
        raise ValueError('Complete stock section terminal boundary missing')
    section = tail[:end]
    if '金额单位：人民币元' not in re.sub(r'\s+', '', section):
        raise ValueError('Stock market-value currency unit is not explicit')
    pattern = re.compile(r'^\s*(\d+)\s+(\d{6})\s+(.+?)\s+([0-9][0-9,]*(?:\.\d+)?)\s+'
                         r'([0-9][0-9,]*(?:\.\d+)?)\s+([0-9]+(?:\.\d+)?)\s*$', re.M)
    records, previous, strategy, strategies = {}, 0, 'default', set()
    for line in section.splitlines():
        if re.match(r'^\s*'+re.escape(anchor[1])+r'\.\d+\s+[^\n]*(?:指数投资|积极投资)', line):
            strategy = re.sub(r'\s+', '', line)
            if strategy in strategies:
                raise ValueError('Repeated strategy section')
            strategies.add(strategy)
            previous = 0
        match = pattern.fullmatch(line)
        if not match:
            if re.match(r'^\s*\d+\s+\d{6}\s+', line):
                raise ValueError('Malformed complete-stock row')
            continue
        rank, code, name, shares, value, ratio = match.groups()
        if int(rank) != previous+1:
            raise ValueError('Complete stock row ordinal missing or duplicated')
        previous = int(rank)
        amount, weight, quantity = numeric(value), numeric(ratio), numeric(shares)
        if amount is None or quantity is None or weight is None or min(amount, quantity, weight) < 0 or weight > 100:
            raise ValueError('Invalid disclosed stock quantities/weight')
        row = records.setdefault(code, {'stock_code': code, 'name': name, 'shares': 0, 'market_value': 0,
                                        'nav_ratio': 0, 'strategies': set()})
        if strategy in row['strategies']:
            raise ValueError('Repeated stock in one strategy table')
        row['strategies'].add(strategy)
        row['shares'] += quantity
        row['market_value'] += amount
        row['nav_ratio'] += weight
    if not records:
        return _disclosed_stock_rows(report)
    if sum(row['nav_ratio'] for row in records.values()) > 100.2:
        raise ValueError('No complete coherent ALL-stock portfolio')
    return [{key: value for key, value in row.items() if key != 'strategies'} for row in records.values()]


def _disclosed_stock_rows(report):
    """Grid/QDII tables explicitly locate A shares; foreign/zero portfolios are verified exclusions."""
    from .product_reports import full_stock_rows  # pylint: disable=import-outside-toplevel
    rows = full_stock_rows(report)
    for row in rows:
        value = row.get('market_value')
        if value is None or value < 0:
            raise ValueError('Complete A-equity fair values lack explicit CNY units')
    return rows


def _absence(product, fund_code, error):
    evidence = {'fund_code': fund_code, **error.evidence}
    version = store.record_disclosure_state(product, fund_code, error.source, evidence, status=error.status)
    raise type(error)(error.reason, error.source, {**evidence, 'eligibility_version': version['version_id']},
                      observed_at=version['observed_at'])


def _etf_role(master):
    if master.get('market') != 'E':
        raise NotApplicableError('not_exchange_fund', 'canonical-fund-market-role', {'ts_code': master['ts_code']})
    body = concepts._source('etf_roles', codes=master['ts_code'])  # pylint: disable=protected-access
    rows = body.get('roles')
    exact_subject = (body.get('coveredSymbols') == [master['ts_code']]
                     and body.get('completeSubjectRoles') is True)
    if (not exact_subject or not isinstance(rows, list) or len(rows) != 1 or rows[0].get('symbol') != master['ts_code']
            or not isinstance(rows[0].get('isETF'), bool)):
        raise ValueError('Exact governed exchange ETF role required')
    if rows[0]['isETF'] is not True or rows[0].get('assetType') != 'ETF':
        raise NotApplicableError('not_exchange_etf', 'governed-exchange-role', rows[0])


def industry_rows(report, stocks, responses, fund_code, name):
    """Current governed SW1 membership × actual annual fair value / gross assets."""
    total_assets = gross_assets(report)
    expected, classifications, observed = {row['stock_code'] for row in stocks}, {}, []
    for body in responses:
        if body.get('source') != 'open_market_products' or body.get('product') != 'stock_industry':
            raise ValueError('Wrong industry classification source')
        items = body.get('items')
        if not isinstance(items, list) or body.get('total') != len(items):
            raise ValueError('Industry subject response is truncated')
        for row in items:
            code = row.get('stockCode')
            if code not in expected or code in classifications or not row.get('industryCodeSwL1'):
                raise ValueError('Missing/duplicate requested stock SW1 identity')
            classifications[code] = (row['industryCodeSwL1'], row.get('industryNameSwL1'))
        observed.append(body)
    if set(classifications) != expected:
        raise ValueError('All disclosed holdings need exact industry classification')
    values = {}
    for stock in stocks:
        code, industry_name = classifications[stock['stock_code']]
        if not industry_name:
            raise ValueError('Industry name not verified')
        row = values.setdefault(code, {'industryCode': code, 'industryName': industry_name, 'industryLevel': 1,
                                       'holdingAssetAnnual': 0, 'holdingNavAnnual': 0})
        row['holdingAssetAnnual'] += stock['market_value']/total_assets*100
        row['holdingNavAnnual'] += stock['nav_ratio']
    return [{'record_key': fund_code+':'+code, 'is_etf': True,
             'data': {'fundCode': fund_code, 'fundName': name, 'publishDate': report['published'],
                      'endDate': report['period'], **{key: round(value, 8) if isinstance(value, float) else value
                                                    for key, value in row.items()}}}
            for code, row in sorted(values.items())]


def _check_reportable(master, product, args):
    if master.get('found_date'):
        when = date.fromisoformat(args.end)
        if product == 142:
            previous = date(when.year if when.month == 12 and when.day == 31 else when.year-1, 12, 31)
        else:
            previous = (date(when.year, 6, 30) if when.month >= 7 else date(when.year-1, 12, 31))
        if date.fromisoformat(master['found_date']) > previous:
            _absence(product, args.fund_code, NotApplicableError(
                'etf_not_established_in_required_completed_full_reporting_period',
                'official-complete-etf-directory-setup-date', {'fund_code': args.fund_code,
                'found_date': master['found_date'], 'required_report_period': previous.isoformat(),
                'directory_version': master['directory_version'],
                'scope_version': master['scope_version']}))



def collect(args):
    """Publish only real issuer portfolio scope; theme tracking is never inferred."""
    from .product_reports import report_index, parse_document, latest_period_versions  # pylint: disable=import-outside-toplevel
    from .product_report_cache import load_report  # pylint: disable=import-outside-toplevel
    from .product_etf_generation import directory_member  # pylint: disable=import-outside-toplevel
    master = directory_member(args.fund_code)
    try:
        _etf_role(master)
    except NotApplicableError as exc:
        raise ValueError('Live role contradicts the complete ETF directory; resynchronize authority') from exc
    product = KINDS[args.kind]
    _check_reportable(master, product, args)
    index = report_index(args.fund_code, identity_validator=directory_member)
    pattern = r'20[0-9]{2}年年度报告' if product == 142 else r'20[0-9]{2}年(?:年度|中期|半年度)报告'
    candidates = [row for row in index if re.search(pattern, row.get('TITLE', ''))]
    if not candidates:
        _absence(product, args.fund_code, NotDisclosedError(
            'required_full_etf_report_not_disclosed', 'official-complete-report-index',
            {'fund_code': args.fund_code, 'index_sha256': collectors.evidence(index),
             'directory_version': master['directory_version'], 'scope_version': master['scope_version'],
             'index_rows': len(index),
             'required_report': 'annual' if product == 142 else 'annual-or-semiannual'}))
    metadata = latest_period_versions(candidates, periodic=True, latest_only=True)[-1]
    report = load_report(metadata, args.fund_code, parser=parse_document, periodic=True)
    stocks = complete_domestic_stocks(report)
    responses = []
    if product == 142:
        for offset in range(0, len(stocks), 20):
            responses.append(concepts._source('stock_industry',  # pylint: disable=protected-access
                             codes=','.join(row['stock_code'] for row in stocks[offset:offset+20]),
                             end_date=date.today().isoformat(), page=1, page_size=100))
        records = industry_rows(report, stocks, responses, args.fund_code, master.get('name'))
        method = 'annual-full-domestic-portfolio-current-sw1-gross-assets-v1'
    else:
        responses = [concepts._source('cls_memberships',  # pylint: disable=protected-access
                     codes=','.join(row['stock_code'] for row in stocks[offset:offset+20]))
                     for offset in range(0, len(stocks), 20)]
        names = {}
        for body in responses:
            for row in body.get('items', []):
                code, name = row.get('conceptCode'), row.get('conceptName')
                valid_code = isinstance(code, str) and re.fullmatch(r'CLS[0-9]+', code)
                valid_name = isinstance(name, str) and bool(name) and (code not in names or names[code] == name)
                if not valid_code or not valid_name:
                    raise ValueError('Exact current CLS concept identity/name is required')
                names[code] = name
        records = concepts.exposure_rows(stocks, responses, names, args.fund_code,
                                        source_identity=('cls_market_products', 'memberships', 'CLS'))
        for record in records:
            record['is_etf'] = True
            record['data'].pop('holdingWeightLatest', None)
            record['data'].update(publishDate=report['published'], endDate=report['period'], conceptCategory='C01',
                                  fundName=master.get('name'))
        concepts.check_observed(responses)
        method = 'full-domestic-etf-portfolio-current-cls-nav-association-v1'
    result = store.ingest(product, records, scope_kind='fund', scope_key=args.fund_code,
                        window_start=report['period'], window_end=report['period'],
                        source_name='issuer:complete-etf-portfolio+governed-classification', method_version=method,
                        evidence_uri='https://pdf.dfcfw.com/pdf/H2_'+metadata['ID']+'_1.pdf',
                        evidence_sha256=collectors.evidence({'report': report['hash'], 'classifications': responses}),
                        complete_window=True, available_fields=FIELDS[product],
                        scope_metadata={'directory_version': master['directory_version'],
                                        'scope_version': master['scope_version']})
    from .product_etf_generation import prune_staging  # pylint: disable=import-outside-toplevel
    prune_staging(product, args.fund_code)
    return result
