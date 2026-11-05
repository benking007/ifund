"""Offline fixed East Money fund disclosure adapters; no eval or request workers."""
from __future__ import annotations

from decimal import Decimal
from html.parser import HTMLParser
import re

from .product_contracts import day


class _Tables(HTMLParser):
    """Extract table cells without executing scripts or trusting embedded markup."""

    def __init__(self):
        super().__init__()
        self.tables = []
        self.current = None
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            self.current = []
        elif tag == 'tr' and self.current is not None:
            self.row = []
        elif tag in ('td', 'th') and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self.cell is not None:
            self.row.append(''.join(self.cell).strip())
            self.cell = None
        elif tag == 'tr' and self.row is not None:
            self.current.append(self.row)
            self.row = None
        elif tag == 'table' and self.current is not None:
            self.tables.append(self.current)
            self.current = None


def _number(value):
    if value is None or str(value).strip() in ('', '--', '---', '—'):
        return None
    text = str(value).strip().replace(',', '').removesuffix('%')
    if not re.fullmatch(r'-?[0-9]+(?:\.[0-9]+)?', text):
        raise ValueError('Unreviewed financial number representation')
    number = Decimal(text)
    if not number.is_finite():
        raise ValueError('Invalid financial observation')
    return number


def asset_rows(document, fund_code, *, latest_before=None):
    """Latest report's NAV ratios. Report date is NOT custodian review date.

    The frozen contract's date means custodian review, so it stays omitted.
    Actual report period is retained in record identity and batch coverage.
    Market capitalization is not inferred by multiplying rounded NAV ratios.
    """
    title = re.search(r'<title>(.*?)</title>', document, re.S | re.I)
    if not title or '('+fund_code+')' not in title[1]:
        raise ValueError('Fund disclosure page identity mismatch')
    parser = _Tables()
    parser.feed(document)
    expected = ['报告期', '股票占净比', '债券占净比', '现金占净比', '存托凭证占净比', '净资产（亿元）']
    tables = [table for table in parser.tables if table and table[0] == expected]
    if len(tables) != 1:
        raise ValueError('Fund asset disclosure table schema changed')
    candidates = []
    for row in tables[0][1:]:
        if len(row) != len(expected):
            raise ValueError('Fund asset disclosure row schema changed')
        period = day(row[0])
        if latest_before and period > latest_before:
            continue
        values = {'fundCode': fund_code}
        for index, field, scale in ((1, 'stockRatio', 1), (2, 'bondRatio', 1), (3, 'cashRatio', 1),
                                     (4, 'depositaryReceiptRatioNav', 1), (5, 'nav', 100000000)):
            value = _number(row[index])
            if value is not None:
                values[field] = float(value*scale)
        if len(values) < 2:
            raise ValueError('Fund asset disclosure has no actual facts')
        candidates.append({'record_key': fund_code+':'+period, 'source_report_period': period, 'data': values})
    return sorted(candidates, key=lambda row: row['source_report_period'])[-1:]


def financial_rows(payload, fund_code):
    """Semiannual/year-end disclosed indicators, yuan and original percentages."""
    if payload.get('ErrCode') != 0 or not isinstance(payload.get('Data'), dict):
        raise ValueError('Financial disclosure provider failed')
    columns = payload['Data'].get('data')
    mapping = {'COMPROFIT': 'profitExclFairValueChange', 'NETPROFIT': 'profitCurrent',
               'UNITPROFIT': 'profitPerShareWeightedAvg', 'NGROWTH': 'navReturnWeightedAvg',
               'FNGROWTH': 'navChangePctCurrent', 'DISPROFIT': 'distributableIncome',
               'DIFUNTIPROFIT': 'distributableIncomePerShare', 'ENDNAV': 'navEnd',
               'ENDUNITNAV': 'navPerShareEnd', 'FCNGROWTH': 'navReturnCumulative'}
    if not isinstance(columns, dict) or not {'FSRQ', *mapping} <= set(columns):
        raise ValueError('Financial disclosure schema changed')
    dates = columns['FSRQ']
    if not isinstance(dates, list) or len(dates) > 100 or any(len(columns[key]) != len(dates) for key in mapping):
        raise ValueError('Financial disclosure column alignment failed')
    result = []
    for index, raw in enumerate(dates):
        period = day(raw)
        report_type = {'06-30': 'I', '12-31': 'F'}.get(period[5:])
        if not report_type:
            raise ValueError('Non-semiannual source period must not be relabelled')
        values = {'fundCode': fund_code, 'reportType': report_type, 'reportPeriodEnd': period}
        for source, target in mapping.items():
            value = _number(columns[source][index])
            if value is not None:
                values[target] = float(value)
        if len(values) == 3:
            raise ValueError('Financial disclosure has no actual indicators')
        result.append({'record_key': fund_code+':'+period, 'data': values})
    return result


def _get(session, endpoint, **kwargs):
    response = session.get(endpoint, timeout=(5, 20), allow_redirects=False, **kwargs)
    response.raise_for_status()
    if response.status_code != 200 or len(response.content) > 4000000:
        raise ValueError('Unexpected disclosure response')
    return response


def collect(kind, fund_code, start, end):
    """Offline invocation; fixed endpoints and <=50 bounded disclosed years."""
    import requests  # pylint: disable=import-outside-toplevel,import-error
    if not re.fullmatch(r'[0-9]{6}', fund_code):
        raise ValueError('Invalid fund identity')
    with requests.Session() as session:
        session.headers.update({'Referer': 'https://fundf10.eastmoney.com/cwzb_'+fund_code+'.html'})
        if kind == 'asset-allocation':
            raw = _get(session, 'https://fundf10.eastmoney.com/zcpz_'+fund_code+'.html').content.decode('utf-8')
            rows = asset_rows(raw, fund_code, latest_before=end)
            if not rows:
                raise ValueError('No verified latest asset report')
            return 259, rows, raw, 'em-latest-disclosed-nav-ratios-v1'
        if kind != 'financial-indicators':
            raise ValueError('Unreviewed disclosure adapter')
        endpoint = 'https://api.fund.eastmoney.com/f10/GetArrayCwzb'
        def fetch(year):
            return _get(session, endpoint, params={'fundcode': fund_code, 'showtype': 0, 'year': year}).json()
        current = fetch('')
        records = financial_rows(current, fund_code)
        years = current['Data'].get('years')
        if not isinstance(years, list) or len(years) > 50 or any(
                isinstance(year, bool) or not isinstance(year, int) or not 1900 <= year <= 2100 for year in years):
            raise ValueError('Disclosure history bounds changed')
        evidence, unique = [current], {row['record_key']: row for row in records}
        for year in years:
            if not int(start[:4]) <= year <= int(end[:4]):
                continue
            payload = fetch(year)
            evidence.append(payload)
            for row in financial_rows(payload, fund_code):
                if row['record_key'] in unique and row != unique[row['record_key']]:
                    raise ValueError('Conflicting financial disclosure revisions')
                unique[row['record_key']] = row
        return 273, list(unique.values()), evidence, 'em-semiannual-reported-indicators-yuan-v1'
