"""Award-organizer primary lists; ratings never become awards or invented IDs."""
from __future__ import annotations

import hashlib
import html
import re

from . import product_additional as observations, product_collectors as collectors, product_store as store

SOURCES = ({'url': 'https://epaper.cs.com.cn/zgzqb/html/2025-12/31/nw.D110000zgzqb_20251231_2-A04.htm',
            'published': '2025-12-31', 'edition': '第22届基金业金牛奖', 'organizer': '中国证券报'},)
FIELDS = {'fundCode', 'fundName', 'awardName', 'awardOrgName', 'awardObjectTypeCode', 'publishDate'}


def _normal(value):
    return re.sub(r'\s+', '', html.unescape(value)).replace('（', '(').replace('）', ')')


def award_rows(document, profile, fund_code, specification):
    """Only exact issuer-profile names match organizer's product recipients."""
    title = re.search(r'<title>(.*?)</title>', profile, re.S | re.I)
    if not title:
        raise ValueError('Source fund identity missing')
    match = re.match(r'(.*?)\('+re.escape(fund_code)+r'\)', html.unescape(title[1]))
    if not match:
        raise ValueError('Explicit fund profile code/name role required')
    name = match[1]
    if not all(term in document for term in ('第22届基金业金牛奖', '获奖名单', '2025年12月31日')):
        raise ValueError('Organizer list edition/publication role changed')
    content = re.search(r'<div\b[^>]*id=["\']?ozoom["\']?\b[^>]*>.*?'
                        r'<founder-content>(.*?)</founder-content>', document, re.S | re.I)
    if not content:
        raise ValueError('Complete organizer result content missing')
    lines = [html.unescape(re.sub(r'<[^>]*>', '', value)).strip()
             for value in re.split(r'<br\s*/?>', content[1], flags=re.I)]
    rows, category, recipients = [], None, 0
    for i, line in enumerate(lines):
        if re.fullmatch(r'[五七]年期开放式[股混债][票合券]型', line):
            if i+1 >= len(lines) or lines[i+1] != '持续优胜金牛基金':
                raise ValueError('Award category is incomplete')
            category = line+'持续优胜金牛基金'
            continue
        if category and line and line != '持续优胜金牛基金':
            recipients += 1
            if _normal(line) == _normal(name):
                row = {'fundCode': fund_code, 'fundName': name,
                       'awardName': specification['edition']+'·'+category,
                       'awardOrgName': specification['organizer'], 'awardObjectTypeCode': 2,
                       'publishDate': specification['published']}
                rows.append({'record_key': hashlib.sha256((row['awardName']+line).encode()).hexdigest(), 'data': row})
    if recipients != 28:
        raise ValueError('Primary list does not contain its complete 28 product recipients')
    return rows


def collect(args):
    """Offline fixed-host acquisition with exact primary-list coverage metadata."""
    from curl_cffi import requests  # pylint: disable=import-outside-toplevel
    observations._master(args.fund_code)  # pylint: disable=protected-access
    profile = requests.get('https://fundf10.eastmoney.com/jbgk_'+args.fund_code+'.html',
                           impersonate='chrome', timeout=30, allow_redirects=False)
    profile.raise_for_status()
    records, proof = [], []
    for source in SOURCES:
        response = requests.get(source['url'], impersonate='chrome', timeout=30, allow_redirects=False)
        response.raise_for_status()
        if len(response.content) > 1000000:
            raise ValueError('Organizer list exceeds reviewed byte bound')
        document = response.content.decode('utf-8')
        records.extend(award_rows(document, profile.content.decode('utf-8'), args.fund_code, source))
        proof.append({'source': source, 'html_sha256': hashlib.sha256(response.content).hexdigest()})
    return store.ingest(242, records, scope_kind='fund', scope_key=args.fund_code,
                        window_start=min(s['published'] for s in SOURCES),
                        window_end=max(s['published'] for s in SOURCES),
                        source_name='china-securities-journal:organizer-primary-result-list',
                        method_version='exact-profile-product-name-22nd-edition-v1', evidence_uri=SOURCES[0]['url'],
                        evidence_sha256=collectors.evidence(proof), complete_window=True,
                        available_fields=FIELDS, ttl_seconds=min(args.ttl_seconds, 86400))
