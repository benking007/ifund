"""Actual issuer periodic reports: identity, class columns, CNY/units and dates."""
from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal
import hashlib
import io
import re

from .product_contracts import day
from .product_numbers import numeric
from .product_identity import canonical_master, not_yet_reportable
from .product_outcomes import NotDisclosedError, NotApplicableError

KINDS = {'quarter-report': 274, 'fof-holdings': 260, 'share-relations': 234, 'share-flows': 266, 'holder-report': 265}


def latest_period_versions(candidates, *, periodic=False, latest_only=False):
    """Reporting period first; later revisions replace that period without duplication."""
    candidates = [row for row in candidates if not re.search(r'报告摘要|提示性公告', row.get('TITLE', ''))]
    if latest_only and candidates:
        latest = max(metadata_period(row, periodic=periodic)[0] for row in candidates)
        candidates = [row for row in candidates if metadata_period(row, periodic=periodic)[0] == latest]
    selected = {}
    for row in candidates:
        period = metadata_period(row, periodic=periodic)[0]
        previous = selected.get(period)
        priority = (row['PUBLISHDATE'], bool(re.search(r'修订|更正|更新', row.get('TITLE', ''))))
        older = ((previous['PUBLISHDATE'], bool(re.search(r'修订|更正|更新', previous.get('TITLE', ''))))
                 if previous else None)
        if previous and priority == older and row['ID'] != previous['ID']:
            raise ValueError('Same-period report revision ordering is not evidenced')
        if previous is None or priority > older:
            selected[period] = row
    return [selected[period] for period in sorted(selected)]


def latest_report(fund_code):
    """Fixed current announcement page and exact issuer PDF, for zero proofs."""
    rows = report_index(fund_code)
    candidates = [row for row in rows if re.search(r'20[0-9]{2}年第[1-4]季度报告', row.get('TITLE', ''))]
    if not candidates:
        if not any(re.search(r'20[0-9]{2}年(?:年度|中期|半年度)报告', row.get('TITLE', '')) for row in rows):
            from .product_collectors import evidence  # pylint: disable=import-outside-toplevel
            raise NotDisclosedError('no_periodic_report_disclosed', 'official-complete-fund-announcement-index',
                                    {'fund_code': fund_code, 'source_rows': len(rows),
                                     'required_report': 'any-periodic', 'sha256': evidence(rows), 'index': rows})
        raise ValueError('Quarter report missing while other periodic disclosure exists')
    metadata = latest_period_versions(candidates, latest_only=True)[-1]
    if not re.fullmatch(r'AN[0-9]{18}', metadata.get('ID', '')):
        raise ValueError('Unexpected current report identifier')
    from .product_report_cache import load_report  # pylint: disable=import-outside-toplevel
    return load_report(metadata, fund_code, parser=parse_document)


def _compact(value):
    return re.sub(r'\s+', '', value or '').replace(':', '：')


def metadata_period(metadata, *, periodic=False):
    """Reporting period from the explicit original report type, before fetching a PDF."""
    match = re.search(r'(20[0-9]{2})年第([1-4])季度报告', metadata.get('TITLE', ''))
    longer = re.search(r'(20[0-9]{2})年(年度|中期|半年度)报告', metadata.get('TITLE', '')) if periodic else None
    if not match and not longer:
        raise ValueError('An actual quarterly/annual/semiannual report title is required')
    year, quarter = (int(match[1]), int(match[2])) if match else (int(longer[1]), 4 if longer[2] == '年度' else 2)
    month = quarter*3
    return date(year, month, calendar.monthrange(year, month)[1]).isoformat(), 'Q'+str(quarter)


def parse_document(content, metadata, fund_code, *, periodic=False):
    """Untrusted PDF is data only; no embedded actions/scripts are executed."""
    import pdfplumber  # pylint: disable=import-outside-toplevel
    if not content.startswith(b'%PDF-') or not 1000 <= len(content) <= 20000000:
        raise ValueError('Expected a bounded genuine PDF report')
    if metadata.get('FUNDCODE') != fund_code or not re.fullmatch(r'AN[0-9]{18}', metadata.get('ID', '')):
        raise ValueError('Report announcement identity mismatch')
    period, quarter = metadata_period(metadata, periodic=periodic)
    published = day(metadata['PUBLISHDATE'])
    if published < period:
        raise ValueError('Report publication precedes its reporting period')
    all_tables, texts = [], []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        if not 1 <= len(pdf.pages) <= 200:
            raise ValueError('Report page budget exceeded')
        for page in pdf.pages:
            texts.append(page.extract_text(layout=True) or '')
            all_tables.extend(page.extract_tables())
    text = '\n'.join(texts)
    compact = _compact(text)
    if _compact(metadata['TITLE']) not in compact:
        raise ValueError('PDF title differs from the exact source announcement')
    main = re.search(r'(?:基金主代码|基金交易代码)([0-9]{6})', compact)
    if not main:
        raise ValueError('Explicit report fund identity missing')
    classes = []
    for table in all_tables:
        for row in table:
            if row and any(word in _compact(row[0]) for word in ('下属分级基金的交易代码', '下属分级基金的基金代码')):
                classes = [_compact(cell) for cell in row[1:] if _compact(cell)]
    if classes and (len(classes) != len(set(classes)) or any(not re.fullmatch(r'[0-9]{6}', c) for c in classes)):
        raise ValueError('Report class roles are ambiguous')
    if fund_code not in (classes or [main[1]]):
        raise ValueError('Requested share class does not occur in the report role table')
    return {'text': text, 'tables': all_tables, 'period': period, 'published': published,
            'quarter': quarter, 'classes': classes or [main[1]], 'main': main[1],
            'hash': hashlib.sha256(content).hexdigest()}


def full_stock_rows(report):
    """Only the report's explicitly ALL-equity section, never a top-ten table."""
    text = _compact(report['text'])
    if not re.search(r'期末按公允价值.{0,35}所有(?:权益|股票)投资明细', text):
        if re.search(r'本基金(?:本)?报告期末未持有股票(?:投资)?[。；;]', text):
            return []
        raise ValueError('A complete all-stock report section is required')
    result, active_columns, previous_rank = [], None, 0
    for table in report['tables']:
        header = ''.join(_compact(cell) for row in table[:4] for cell in row)
        declared = ('公允价值' in header and '净值比' in header and '数量' in header
                    and any(word in header for word in ('证券代码', '股票代码')))
        if declared and '份额' not in header and '基金代码' not in header:
            active_columns = len(table[0])
        elif active_columns is None or len(table[0]) != active_columns:
            continue
        data = [row for row in table if row and re.fullmatch(r'[0-9]+', _compact(row[0]))]
        for row in data:
            cells = [_compact(cell) for cell in row]
            rank = int(cells[0])
            if rank != previous_rank+1:
                raise ValueError('Full-stock disclosure row sequence is incomplete')
            previous_rank = rank
            if len(cells) == 9:  # Explicit market/country in QDII all-equity report.
                token = re.fullmatch(r'([0-9]{1,6})CH', cells[3])
                if not token:
                    # The source's explicit local-market suffix is authoritative;
                    # an unknown suffix may not accidentally become an A share.
                    if re.fullmatch(r'.+(?:HK|US|KS|JP|TT|LN)', cells[3]):
                        continue
                    raise ValueError('Unreviewed explicit equity market role')
                if cells[5] != '中国内地':
                    raise ValueError('CH local stock identity/country conflict')
                code, ratio = token[1].zfill(6), numeric(cells[8])
                markets = {'上海证': 'SH', '深圳证': 'SZ', '北京证': 'BJ'}
                market = next((suffix for label, suffix in markets.items() if cells[4].startswith(label)), None)
                if market is None:
                    raise ValueError('Explicit mainland exchange role is missing')
            elif len(cells) == 6:
                code, ratio = cells[1], numeric(cells[5])
                if not re.fullmatch(r'[0-9]{6}', code):
                    raise ValueError('A-share disclosure stock code must be explicit')
            else:
                raise ValueError('Full-stock report column schema changed')
            if ratio is None or not 0 <= ratio <= 100:
                raise ValueError('Full-stock NAV proportion is invalid')
            value = {'stock_code': code, 'nav_ratio': ratio,
                     **({'symbol': code+'.'+market} if len(cells) == 9 else {})}
            cny = ('人民币元' in header or '金额单位：人民币元' in text)
            if cny:
                value['market_value'] = numeric(cells[7] if len(cells) == 9 else cells[4])
                value['shares'] = numeric(cells[6] if len(cells) == 9 else cells[3])
            result.append(value)
    if (not previous_rank or len({row['stock_code'] for row in result}) != len(result)
            or sum(row['nav_ratio'] for row in result) > 100.1):
        raise ValueError('All-stock disclosure is missing, duplicated or inconsistent')
    return result


def financial_rows(report, fund_code):
    """Exact 3.1 quarter profit/NAV table, selecting its declared class column."""
    text = report['text']
    section = re.search(r'3\.1\s*主要财务指标(.*?)3\.2\s*基金净值表现', text, re.S)
    if not section or '单位：人民币元' not in _compact(section[1]):
        raise ValueError('Quarter financial section/CNY unit missing')
    candidates = [table for table in report['tables'] if any(
        row and '本期已实现收益' in _compact(row[0]) for row in table)]
    if len(candidates) != 1:
        raise ValueError('Quarter financial table is missing or duplicated')
    column = report['classes'].index(fund_code)+1
    values = {'fundCode': fund_code, 'publishDate': report['published'], 'reportPeriodEnd': report['period'],
              'reportType': report['quarter']}
    mapping = {'本期已实现收益': 'profitExclFairValueChange', '本期利润': 'profitCurrent',
               '加权平均基金份额本期利润': 'profitPerShareWeightedAvg',
               '期末基金资产净值': 'navEnd', '期末基金份额净值': 'navPerShareEnd'}
    seen = set()
    for row in candidates[0]:
        label = re.sub(r'^[1-5][.、．]', '', _compact(row[0]))
        if label not in mapping:
            continue
        if label in seen or len(row) != len(report['classes'])+1:
            raise ValueError('Quarter class column count or financial identity changed')
        value = numeric(row[column])
        if value is None:
            raise ValueError('Required disclosed financial observation is missing')
        values[mapping[label]] = value
        seen.add(label)
    if set(mapping) != seen:
        raise ValueError('Quarter financial table is incomplete')
    values['fairValueChangeIncome'] = float(Decimal(str(values['profitCurrent']))-
                                          Decimal(str(values['profitExclFairValueChange'])))
    return [{'record_key': fund_code+':'+report['period'], 'data': values}]


def relationship_rows(report, fund_code):
    """Only an explicitly listed same-level share-class relation; no guessed ETF link."""
    if len(report['classes']) < 2:
        raise ValueError('No actual same-level class relation is disclosed')
    return [{'record_key': fund_code+':'+code+':10', 'data': {
        'fundCode': fund_code, 'relatedFundCode': code, 'relationType': 10, 'endDate': report['period']}}
        for code in report['classes'] if code != fund_code]


def fof_rows(report, fund_code):
    """Actual disclosed fund-investment rows; never reinterpret stock holdings."""
    result = []
    for table in report['tables']:
        header_rows = []
        for row in table:
            if row and re.fullmatch(r'[0-9]+', _compact(row[0])):
                break
            header_rows.append(row)
        header = ''.join(_compact(cell) for row in header_rows for cell in row)
        if not all(word in header for word in ('基金代码', '持有份额', '公允价值', '净值比')):
            continue
        if '份' not in header or '元' not in header:
            raise ValueError('FOF share/value units missing')
        for row in table:
            if len(row) < 7 or not re.fullmatch(r'[0-9]+', _compact(row[0])):
                continue
            code = _compact(row[1])
            if not re.fullmatch(r'[0-9]{6}', code):
                raise ValueError('FOF holding fund identifier is not an actual code')
            shares, value, ratio = (numeric(_compact(cell)) for cell in row[4:7])
            if any(number is None or number < 0 for number in (shares, value, ratio)) or ratio > 100:
                raise ValueError('FOF financial fact out of bounds')
            result.append({'record_key': fund_code+':'+report['period']+':'+code, 'data': {
                'fundCode': fund_code, 'date': report['published'], 'holdingFundCode': code,
                'holdingShares': shares, 'holdingValue': value, 'holdingRatio': ratio}})
    if not result or len({row['record_key'] for row in result}) != len(result):
        raise ValueError('No complete unambiguous disclosed FOF holding table')
    return result


def share_rows(report, fund_code):
    """Original report's complete share flows; don't derive flows from NAV or totals."""
    mapping = {'报告期期初基金份额总额': 'beginShare', '报告期期间基金总申购份额': 'subscribeShare',
               '减：报告期期间基金总赎回份额': 'redeemShare', '报告期期末基金份额总额': 'endShare'}
    column = report['classes'].index(fund_code)+1
    values = {'fundCode': fund_code, 'endDate': report['period']}
    for table in report['tables']:
        for row in table:
            if not row or _compact(row[0]) not in mapping:
                continue
            cells = [cell for cell in row[1:] if _compact(cell)]
            if len(cells) != len(report['classes']):
                raise ValueError('Fund share-flow class column mismatch')
            field, value = mapping[_compact(row[0])], numeric(cells[column-1])
            if value is None or value < 0 or field in values:
                raise ValueError('Missing, negative or conflicting disclosed share flows')
            values[field] = value
    if set(mapping.values())-set(values):
        raise ValueError('Complete report share-flow statement is required')
    return [{'record_key': fund_code+':'+report['period'], 'data': values}]


def holder_rows(report, fund_code):
    """Exact CNY fund report's unit-share holder counts, never rounded billion shares."""
    if len(report['classes']) != 1 or report['classes'][0] != fund_code:
        raise ValueError('This holder-table adapter requires one explicitly identified share class')
    tables = [table for table in report['tables'] if table and len(table[0]) == 6
              and '持有人户数' in _compact(table[0][0]) and '户均持有' in _compact(table[0][1])]
    if len(tables) != 1:
        raise ValueError('An unambiguous exact holder-count table is required')
    table = tables[0]
    header = ''.join(_compact(cell) for row in table[:3] for cell in row)
    if '机构投资者' not in header or '个人投资者' not in header:
        raise ValueError('Holder role columns changed')
    data = [row for row in table[3:] if row and numeric(_compact(row[0])) is not None]
    if len(data) != 1 or len(data[0]) != 6:
        raise ValueError('Holder data/class role changed')
    fields = ('holderCount', 'avgHoldingShare', 'instHoldingShare', 'instHoldingRatio',
              'retailHoldingShare', 'retailHoldingRatio')
    values = {'fundCode': fund_code, 'publishDate': report['published'], 'reportDate': report['period'],
              'infoSourceCode': 6 if report['period'].endswith('12-31') else 3,
              'infoSource': '年报' if report['period'].endswith('12-31') else '中报'}
    values.update({key: numeric(_compact(value)) for key, value in zip(fields, data[0])})
    if any(value is None or value < 0 for key, value in values.items() if key in fields):
        raise ValueError('Invalid exact holder amount')
    if abs(values['instHoldingRatio']+values['retailHoldingRatio']-100) > .02:
        raise ValueError('Holder role percentages do not reconcile')
    for table in report['tables']:
        for row in table:
            if row and _compact(row[0]) == '基金管理人所有从业人员持有本基金' and len(row) == 3:
                if 'staffHoldingShare' in values:
                    raise ValueError('Duplicate employee holder observation')
                values['staffHoldingShare'], values['staffHoldingRatio'] = map(numeric, map(_compact, row[1:]))
    return [{'record_key': fund_code+':'+report['period'], 'data': values}]


def report_index(fund_code, *, identity_validator=None):
    """Obtain the entire bounded periodic-report index before declaring non-disclosure."""
    from curl_cffi import requests  # pylint: disable=import-outside-toplevel
    (identity_validator or canonical_master)(fund_code)
    endpoint = 'https://api.fund.eastmoney.com/f10/JJGG'
    rows = []
    total = None
    for page in range(1, 21):
        response = requests.get(endpoint, params={'fundcode': fund_code, 'pageIndex': page,
                                                  'pageSize': 100, 'type': 3},
                                headers={'Referer': 'https://fundf10.eastmoney.com/jjgg_'+fund_code+'_3.html'},
                                timeout=30,
                                impersonate='chrome', allow_redirects=False)
        response.raise_for_status()
        if len(response.content) > 8000000:
            raise ValueError('Announcement list byte bound exceeded')
        payload = response.json()
        current = payload.get('Data')
        if (payload.get('ErrCode') != 0 or not isinstance(payload.get('TotalCount'), int)
                or isinstance(payload.get('TotalCount'), bool)
                or payload.get('PageIndex') != page or not isinstance(current, list)):
            raise ValueError('Report list identity/pagination protocol changed')
        if any(row.get('FUNDCODE') != fund_code for row in current):
            raise ValueError('Report list contains a different fund identity')
        total = payload['TotalCount'] if total is None else total
        if total != payload['TotalCount']:
            raise ValueError('Report list changed while collecting')
        rows.extend(current)
        if len(rows) == total:
            break
        if not current or len(rows) > total:
            raise ValueError('Report list completeness failed')
    if len(rows) != total or len({row['ID'] for row in rows}) != len(rows):
        raise ValueError('Report list incomplete or duplicated')
    return rows



def collect(args):
    """Fixed official disclosure transport, bounded report list and genuine PDFs."""
    from . import product_store as store, product_collectors as collectors  # pylint: disable=import-outside-toplevel
    master = canonical_master(args.fund_code)
    proof = not_yet_reportable(master, args.end, semiannual=args.kind == 'holder-report')
    if proof:
        raise NotApplicableError('fund_not_established_in_completed_reporting_period',
                                 'official-canonical-fund-inception', proof)
    endpoint = 'https://api.fund.eastmoney.com/f10/JJGG'
    rows = report_index(args.fund_code)
    function = {'quarter-report': financial_rows, 'fof-holdings': fof_rows,
                'share-relations': relationship_rows, 'share-flows': share_rows,
                'holder-report': holder_rows}[args.kind]
    pattern = (r'20[0-9]{2}年(?:年度|中期|半年度)报告' if args.kind == 'holder-report' else
               r'20[0-9]{2}年第[1-4]季度报告')
    candidates = [row for row in rows if re.search(pattern, row.get('TITLE', ''))]
    if not candidates:
        raise NotDisclosedError('no_periodic_report_disclosed', 'official-complete-fund-announcement-index',
                                {'fund_code': args.fund_code, 'source_rows': len(rows),
                                 'sha256': collectors.evidence(rows)})
    # Snapshot products use the newest actual report, never gather multiple
    # reports into indistinguishable current relation/holding rows.
    if args.kind not in {'quarter-report', 'holder-report'}:
        candidates = latest_period_versions(candidates, latest_only=True)[-1:]
    if args.kind in {'quarter-report', 'holder-report'}:
        candidates = [row for row in candidates if day(args.start) <=
                      metadata_period(row, periodic=args.kind == 'holder-report')[0] <= day(args.end)]
    candidates = latest_period_versions(candidates, periodic=args.kind == 'holder-report')
    from .product_report_cache import load_report  # pylint: disable=import-outside-toplevel
    records, evidence = [], []
    for metadata in candidates:
        if not re.fullmatch(r'AN[0-9]{18}', metadata.get('ID', '')):
            raise ValueError('Unexpected report identifier')
        historical = args.kind in {'quarter-report', 'holder-report'}
        period, _ = metadata_period(metadata, periodic=args.kind == 'holder-report')
        if historical and not day(args.start) <= period <= day(args.end):
            continue
        uri = 'https://pdf.dfcfw.com/pdf/H2_'+metadata['ID']+'_1.pdf'
        report = load_report(metadata, args.fund_code, parser=parse_document, periodic=args.kind == 'holder-report')
        current = function(report, args.fund_code)
        records.extend(current)
        evidence.append({'uri': uri, 'sha256': report['hash'], 'period': report['period'],
                         'published': report['published'], 'shared_class_portfolio': len(report['classes']) > 1})
    if not records:
        raise NotDisclosedError('no_periodic_report_in_requested_window',
                                'official-complete-fund-announcement-index',
                                {'fund_code': args.fund_code, 'source_rows': len(rows),
                                 'sha256': collectors.evidence(rows), 'start': args.start, 'end': args.end})
    dates = [row['data'].get('reportPeriodEnd') or row['data'].get('reportDate') or row['data'].get('date')
             or row['data']['endDate'] for row in records]
    historical = args.kind in {'quarter-report', 'holder-report'}
    beginning, ending = (day(args.start), day(args.end)) if historical else (min(dates), max(dates))
    return store.ingest(KINDS[args.kind], records, scope_kind='fund', scope_key=args.fund_code,
                        window_start=beginning, window_end=ending,
                        source_name='issuer:periodic-report',
                        method_version='exact-issuer-report-class-columns-v1',
                        evidence_uri=endpoint, evidence_sha256=collectors.evidence(evidence),
                        complete_window=True, ttl_seconds=min(args.ttl_seconds, 86400))
