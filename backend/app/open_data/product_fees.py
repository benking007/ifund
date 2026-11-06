"""Exact disclosed fee tiers; observation dates never replace execution dates."""
from __future__ import annotations

import hashlib
import html
import re

from . import product_eastmoney as tables


def _text(value):
    return html.unescape(re.sub(r'<[^>]+>', ' ', value)).strip()


def criterion(text, index=1):
    """Parse explicit Chinese interval boundaries without inventing units."""
    value = text.strip().replace('，', ',').replace(' ', '')
    if value in {'---', '--', '不限', '无', ''}:
        return {}
    matches = list(re.finditer(r'(大于等于|小于等于|大于|小于)([0-9]+(?:\.[0-9]+)?)(万元|元|天|日|个月|月|年)', value))
    if not matches or ','.join(match[0] for match in matches) != value:
        return {'chargeCriteria'+str(index)+'Desc': text}
    units = {'万元': 4, '元': 7, '天': 3, '日': 3, '个月': 2, '月': 2, '年': 1}
    if len({units[match[3]] for match in matches}) != 1:
        raise ValueError('Mixed fee interval units')
    suffix = str(index)
    result = {'chargeCriteria'+suffix+'Desc': text, 'chargeCriteria'+suffix+'Unit': units[matches[0][3]]}
    inclusive = [False, False]
    for match in matches:
        side = 'Start' if match[1].startswith('大') else 'End'
        key = 'chargeCriteriaRange'+side+suffix
        if key in result:
            raise ValueError('Duplicate fee interval bound')
        result[key] = float(match[2])
        inclusive[0 if side == 'Start' else 1] = '等于' in match[1]
    result['isIncludeRangeStart'+suffix] = int(inclusive[0])*2+int(inclusive[1])
    return result


def fee_rows(document, fund_code):
    """Preserve original issuer fees, not sales-channel promotional discounts."""
    title = re.search(r'<title>(.*?)</title>', document, re.S | re.I)
    if not title or '('+fund_code+')' not in html.unescape(title[1]):
        raise ValueError('Fee page security identity mismatch')
    headings = list(re.finditer(r'<h4\b[^>]*>(.*?)</h4>', document, re.S | re.I))
    result = []
    for i, heading in enumerate(headings):
        category = _text(heading[1])
        if category not in {'认购费率', '申购费率', '赎回费率', '认购费率（前端）', '认购费率（后端）',
                            '申购费率（前端）', '赎回费率（前端）', '赎回费率（后端）'}:
            continue
        block = document[heading.end():headings[i+1].start() if i+1 < len(headings) else len(document)]
        parser = tables._Tables()  # pylint: disable=protected-access
        parser.feed(block)
        if not parser.tables:
            raise ValueError('Fee tier table disappeared')
        rows = parser.tables[0]
        columns = rows[0]
        if not any('费率' in label for label in columns):
            raise ValueError('Fee header schema changed')
        for row in rows[1:]:
            if len(row) != len(columns):
                raise ValueError('Fee row/header alignment changed')
            rate = re.match(r'\s*(?:每笔)?([0-9]+(?:\.[0-9]+)?)(%|元)', row[-1])
            if not rate and row[-1].strip() in {'不收取', '不收取认购费', '不收取申购费', '不收取赎回费'}:
                rate = re.match(r'([0-9]+)(%)', '0%')
            if not rate:
                raise ValueError('Unproven fee rate or fee unit')
            values = {'fundCode': fund_code, 'dataSource': '东方财富F10原费率披露',
                      'chargeCategoryDesc': category, 'chargeRateMin': float(rate[1]),
                      'chargeRateMax': float(rate[1]), 'chargeUnit': 6 if rate[2] == '%' else 7,
                      'chargeRangeDesc': '；'.join(row[:-1]), 'chargeDesc': row[-1],
                      'chargeDivisionDesc': '原费率，不采用渠道优惠价'}
            for index, text in enumerate(row[:-1], 1):
                values.update(criterion(text, index))
            natural = hashlib.sha256((category+'\n'+'\n'.join(row)).encode()).hexdigest()
            result.append({'record_key': natural, 'data': values})
    if not result:
        raise ValueError('No actual fee tier observations or explicit zero-charge disclosure')
    return result
