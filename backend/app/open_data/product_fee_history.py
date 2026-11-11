"""Original fee-change announcements, exact legal identity and execution date."""
from __future__ import annotations

from datetime import date
import hashlib
import io
import re

from .product_contracts import day
from .product_fees import _text
from .product_numbers import numeric

METHOD = 'issuer-fee-change-original-table-published-executed-v1'
FIELDS = {'fundCode', 'publishDate', 'executeDate', 'dataSource', 'chargeCategoryDesc',
          'chargeRateMin', 'chargeRateMax', 'chargeUnit', 'chargeDesc', 'chargeDivisionDesc'}


def fee_change_rows(content, profile, metadata, fund_code):
    """Don't turn an observation date into the date of an actual fee amendment."""
    import pdfplumber  # pylint: disable=import-outside-toplevel
    title = re.search(r'<title>(.*?)</title>', profile, re.S | re.I)
    name = re.search(r'<th>基金全称</th>.*?<td[^>]*>(.*?)</td>', profile, re.S | re.I)
    if not title or '('+fund_code+')' not in title[1] or not name or metadata.get('FUNDCODE') != fund_code:
        raise ValueError('Exact fee-announcement fund identity required')
    legal_name = re.sub(r'\s+', '', _text(name[1]))
    if not content.startswith(b'%PDF-') or not 1000 <= len(content) <= 20000000:
        raise ValueError('Genuine bounded fee-announcement PDF required')
    text, tables = '', []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        if len(pdf.pages) > 100:
            raise ValueError('Fee-announcement page budget exceeded')
        for page in pdf.pages:
            text += page.extract_text() or ''
            tables.extend(page.extract_tables())
    compact = re.sub(r'\s+', '', text)
    dates = set(re.findall(r'自(20[0-9]{2})年([0-9]{1,2})月([0-9]{1,2})日起', compact))
    if len(dates) != 1 or not all(label in compact for label in ('调整前', '调整后', '管理费率', '托管费率')):
        raise ValueError('Reviewed original old/new fee-change table and exact execution date required')
    year, month, number = map(int, next(iter(dates)))
    execute, published = date(year, month, number).isoformat(), day(metadata['PUBLISHDATE'])
    if execute < published:
        raise ValueError('This reviewed fee amendment cannot precede its announcement')
    matches = [row for table in tables for row in table if len(row) == 6
               and re.fullmatch(r'[0-9]+', re.sub(r'\s+', '', row[0] or ''))
               and re.sub(r'\s+', '', row[1] or '') == legal_name]
    if len(matches) != 1:
        raise ValueError('Requested legal fund name is not unique in this fee amendment')
    row = matches[0]
    result = []
    for category, previous, current in (('管理费率', row[2], row[4]), ('托管费率', row[3], row[5])):
        if not all(re.fullmatch(r'[0-9]+(?:\.[0-9]+)?%', re.sub(r'\s+', '', value))
                   for value in (previous, current)):
            raise ValueError('Original annual percentage fee units missing')
        rate = numeric(current)
        if rate is None or not 0 <= rate <= 100:
            raise ValueError('Invalid original fee rate')
        value = {'fundCode': fund_code, 'publishDate': published, 'executeDate': execute,
                 'dataSource': '基金管理人原始费率变更公告', 'chargeCategoryDesc': category,
                 'chargeRateMin': rate, 'chargeRateMax': rate, 'chargeUnit': 6,
                 'chargeDesc': '年费率由'+previous+'调整为'+current,
                 'chargeDivisionDesc': '原公告调整后年费率；非渠道优惠或申购折扣'}
        key = hashlib.sha256((fund_code+':'+published+':'+execute+':'+category).encode()).hexdigest()
        result.append({'record_key': key, 'data': value})
    return result


def collect(args):
    """Scan the complete bounded official index; unknown fee formats fail closed."""
    from curl_cffi import requests  # pylint: disable=import-outside-toplevel
    from . import product_store as store, product_collectors as collectors  # pylint: disable=import-outside-toplevel
    from .product_identity import canonical_master as _master  # pylint: disable=import-outside-toplevel
    _master(args.fund_code)
    endpoint = 'https://api.fund.eastmoney.com/f10/JJGG'
    start, end = day(args.start), day(args.end)
    announcements, total = [], None
    for page in range(1, 21):
        response = requests.get(endpoint, params={'fundcode': args.fund_code, 'pageIndex': page,
                                                  'pageSize': 500, 'type': 0},
                                headers={'Referer': 'https://fundf10.eastmoney.com/jjgg_'+args.fund_code+'.html'},
                                impersonate='chrome',
                                timeout=30, allow_redirects=False)
        response.raise_for_status()
        if len(response.content) > 8000000:
            raise ValueError('Announcement page byte budget exceeded')
        body = response.json()
        if body.get('ErrCode') != 0 or body.get('PageIndex') != page or not isinstance(body.get('Data'), list):
            raise ValueError('Announcement index pagination changed')
        total = body['TotalCount'] if total is None else total
        if total != body.get('TotalCount') or not isinstance(total, int) or isinstance(total, bool):
            raise ValueError('Announcement source total changed')
        if any(row.get('FUNDCODE') != args.fund_code for row in body['Data']):
            raise ValueError('Announcement source contains a different fund')
        announcements.extend(body['Data'])
        if len(announcements) == total:
            break
        if not body['Data'] or len(announcements) > total:
            raise ValueError('Announcement index truncated')
    if len(announcements) != total or len({row['ID'] for row in announcements}) != total:
        raise ValueError('Announcement index completeness failed')
    candidates = [row for row in announcements if start <= day(row['PUBLISHDATE']) <= end
                  and re.search(r'(?:调低|调整|降低).{0,20}费率', row['TITLE'])]
    if len(candidates) > 50:
        raise ValueError('Fee-amendment PDF budget exceeded')
    response = requests.get('https://fundf10.eastmoney.com/jbgk_'+args.fund_code+'.html',
                            impersonate='chrome', timeout=30, allow_redirects=False)
    response.raise_for_status()
    profile, records, evidence = response.content.decode('utf-8'), [], []
    for metadata in candidates:
        if not re.fullmatch(r'AN[0-9]{18}', metadata.get('ID', '')):
            raise ValueError('Unreviewed announcement identifier')
        uri = 'https://pdf.dfcfw.com/pdf/H2_'+metadata['ID']+'_1.pdf'
        response = requests.get(uri, impersonate='chrome', timeout=60, allow_redirects=False)
        response.raise_for_status()
        records.extend(fee_change_rows(response.content, profile, metadata, args.fund_code))
        evidence.append({'uri': uri, 'sha256': hashlib.sha256(response.content).hexdigest()})
    return store.ingest(243, records, scope_kind='fund', scope_key=args.fund_code,
                        window_start=start, window_end=end, source_name='issuer:fee-amendments', method_version=METHOD,
                        evidence_uri=endpoint, evidence_sha256=collectors.evidence({'index': announcements,
                                                                                   'pdfs': evidence}),
                        complete_window=True, available_fields=FIELDS)
