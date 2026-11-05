"""Versioned offline calculations from actual dated financial observations."""
from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal
import math
import re

from .product_contracts import day


def _positive(value):
    if isinstance(value, bool):
        raise ValueError('Boolean observation')
    number = Decimal(str(value))
    if not number.is_finite() or number <= 0:
        raise ValueError('A positive actual observation is required')
    return number


def premium_rows(prices, navs, fund_code):
    """Same-day raw market close versus real unit NAV; no stale NAV fill."""
    def observations(rows, key):
        result = {}
        for row in rows:
            when = day(row['date'])
            if row.get('fundCode') != fund_code or when in result:
                raise ValueError('Ambiguous premium observation identity')
            result[when] = _positive(row[key])
        return result
    close, unit = observations(prices, 'closePrice'), observations(navs, 'unitNav')
    # A missing NAV is not a zero-row result or a previous day's replacement.
    if not set(close) <= set(unit):
        raise ValueError('A same-date real unit NAV is required for every close')
    return [{'record_key': fund_code+':'+when, 'is_etf': True,
             'data': {'fundCode': fund_code, 'date': when, 'closePrice': float(value),
                      'unitNav': float(unit[when]), 'premiumRate': float((value/unit[when]-1)*100)}}
            for when, value in sorted(close.items())]


def _shift_months(when, months):
    year, month = divmod(when.year*12+when.month-1-months, 12)
    return date(year, month+1, min(when.day, calendar.monthrange(year, month+1)[1]))


def correlation_rows(navs, quotes, fund_code, index_code, *, min_returns=60):
    """Pearson of aligned period-to-period total-NAV/index-price returns.

    A return is calculated after aligning price dates, so it spans the same
    interval on both sides. Six months means six calendar months, not 180
    samples. A missing historical start or flat series is omitted, never 0.
    """
    def observations(rows, field, identity, expected):
        result = {}
        for row in rows:
            when = day(row['date'])
            if row.get(identity) != expected or when in result:
                raise ValueError('Ambiguous correlation observation identity')
            result[when] = float(_positive(row[field]))
        return result
    fund = observations(navs, 'adjNav', 'fundCode', fund_code)
    index = observations(quotes, 'closePrice', 'indexCode', index_code)
    if not fund or not index:
        raise ValueError('Two real observation series are required')
    end = max(fund)
    if end not in index:
        raise ValueError('Benchmark and NAV endpoints must match')
    values = {'fundCode': fund_code, 'date': end, 'indexCode': index_code}
    common = sorted(set(fund) & set(index))
    for months, field in ((6, 'corr6m'), (12, 'corr1y'), (36, 'corr3y'), (60, 'corr5y')):
        start = _shift_months(date.fromisoformat(end), months).isoformat()
        prior = [when for when in common if when <= start]
        if not prior or (date.fromisoformat(start)-date.fromisoformat(prior[-1])).days > 7:
            continue
        window = [when for when in common if prior[-1] <= when <= end]
        if len(window)-1 < min_returns:
            continue
        left = [fund[b]/fund[a]-1 for a, b in zip(window, window[1:])]
        right = [index[b]/index[a]-1 for a, b in zip(window, window[1:])]
        avg_left, avg_right = sum(left)/len(left), sum(right)/len(right)
        covariance = sum((a-avg_left)*(b-avg_right) for a, b in zip(left, right))
        variance = sum((a-avg_left)**2 for a in left)*sum((b-avg_right)**2 for b in right)
        if variance > 0:
            values[field] = max(-1.0, min(1.0, covariance/math.sqrt(variance)))
    if len(values) == 3:
        raise ValueError('No adequately covered nonconstant correlation interval')
    return [{'record_key': fund_code+':'+index_code+':'+end, 'data': values}]


def manager_history_rows(raw, fund_code):
    """Preserve a manager team's published tenure return as a snapshot.

    A joint-name string is not split into individual managers. Legacy null
    end dates do not by themselves prove current employment. The date is the
    actual source fetch day; this calculation must not be used as PIT history.
    """
    result = []
    for row in raw:
        if row.get('fund_code') != fund_code or not row.get('managers') or not row.get('fetch_time'):
            raise ValueError('Manager source identity or snapshot date is missing')
        value = row.get('tenure_return')
        if value is None or isinstance(value, bool) or not math.isfinite(float(value)):
            raise ValueError('An actual reported tenure return is required')
        values = {'fundCode': fund_code, 'fundManagerName': row['managers'], 'date': day(row['fetch_time']),
                  'returnTenure': float(value)}
        for source, target in (('start_date', 'tenureStartDate'), ('end_date', 'tenureEndDate')):
            if row.get(source):
                values[target] = day(row[source])
        if row.get('end_date'):
            values['isIncumbent'] = 0
        result.append({'record_key': fund_code+':'+str(row['seq'])+':'+values['date'], 'data': values})
    return result


def benchmark_rows(text, fund_code):
    """Parse only an explicitly weighted additive benchmark, no code guessing."""
    if not isinstance(text, str) or not 0 < len(text) <= 4096:
        raise ValueError('A source benchmark formula is required')
    components = text.replace('＋', '+').split('+')
    parsed = []
    for component in components:
        match = re.fullmatch(r'\s*(.+?)\s*[×*]\s*([0-9]+(?:\.[0-9]+)?)%\s*', component)
        if not match:
            raise ValueError('Benchmark formula is not an explicitly weighted sum')
        weight = Decimal(match[2])
        if not 0 <= weight <= 100:
            raise ValueError('Invalid benchmark component weight')
        parsed.append((match[1], weight))
    if sum(weight for _, weight in parsed) != 100:
        raise ValueError('Benchmark weights do not form a complete portfolio')
    return [{'record_key': fund_code+':'+str(index), 'data': {'fundCode': fund_code, 'serialNo': index,
            'indexName': name, 'benchmarkWeightPct': float(weight)}}
            for index, (name, weight) in enumerate(parsed, 1)]
