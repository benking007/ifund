"""Reproducible daily-NAV risk/return calculations with explicit benchmark role."""
from __future__ import annotations

import calendar
from bisect import bisect_right
from datetime import date
import math
import statistics

from .product_contracts import day

METHOD = 'aligned-adjusted-nav-benchmark-calendar-risk-v3-rf0-252'
WINDOWS = {'1w': 7, '1m': 1, '3m': 3, '6m': 6, '1y': 12, '3y': 36, '5y': 60}
METRICS = ('return', 'returnAnn', 'returnSimpleAnn', 'volatilityAnn', 'volatilityDownsideAnn',
           'maxDrawdown', 'beta', 'sharpeRatio', 'sortinoRatio', 'calmarRatio', 'omegaRatio',
           'informationRatio', 'treynorRatio', 'm2Ratio', 'trackingError')
FIELDS = {'fundCode', 'endDate'} | {prefix+suffix for prefix in METRICS for suffix in (*WINDOWS, 'Ytd')}


def _series(rows):
    result = {}
    for row in rows:
        when = day(row['date'])
        value = row['value']
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError('Actual finite positive observation required')
        if when in result:
            raise ValueError('Duplicate observed date')
        result[when] = value
    if not 2 <= len(result) <= 20000:
        raise ValueError('Bounded genuine history required')
    return result


def cutoff(end, suffix):
    """Calendar intervals; never interpret one month as thirty observations."""
    if suffix == 'Ytd':
        return date(end.year, 1, 1)
    if suffix == '1w':
        return date.fromordinal(end.toordinal()-7)
    year, month = divmod(end.year*12+end.month-1-WINDOWS[suffix], 12)
    return date(year, month+1, min(end.day, calendar.monthrange(year, month+1)[1]))


def _window_metrics(points, fund, benchmark):
    elapsed = (date.fromisoformat(points[-1])-date.fromisoformat(points[0])).days
    result = {}
    gain = fund[points[-1]]/fund[points[0]]-1
    result.update({'return': gain*100, 'returnAnn': ((1+gain)**(365.25/elapsed)-1)*100,
                   'returnSimpleAnn': gain*365.25/elapsed*100})
    left = [fund[b]/fund[a]-1 for a, b in zip(points, points[1:])]
    right = [benchmark[b]/benchmark[a]-1 for a, b in zip(points, points[1:])]
    peak, drawdown = fund[points[0]], 0
    for when in points:
        peak = max(peak, fund[when])
        drawdown = max(drawdown, 1-fund[when]/peak)
    result['maxDrawdown'] = drawdown*100
    if len(left) < 20:
        return result
    mean, mean_b = statistics.mean(left), statistics.mean(right)
    sd, sd_b = statistics.stdev(left), statistics.stdev(right)
    vol, downside = sd*math.sqrt(252), math.sqrt(statistics.mean(min(value, 0)**2 for value in left))*math.sqrt(252)
    excess = [a-b for a, b in zip(left, right)]
    tracking = statistics.stdev(excess)*math.sqrt(252)
    result.update(volatilityAnn=vol*100, volatilityDownsideAnn=downside*100, trackingError=tracking*100)
    covariance = sum((a-mean)*(b-mean_b) for a, b in zip(left, right))/(len(left)-1)
    beta = covariance/(sd_b**2) if sd_b else None
    if beta is not None:
        result['beta'] = beta
    if sd:
        result['sharpeRatio'] = mean/sd*math.sqrt(252)
        result['m2Ratio'] = result['sharpeRatio']*sd_b*math.sqrt(252)*100
    if downside:
        result['sortinoRatio'] = mean*252/downside
    if drawdown:
        result['calmarRatio'] = result['returnAnn']/100/drawdown
    downside_sum = -sum(min(value, 0) for value in left)
    if downside_sum:
        result['omegaRatio'] = sum(max(value, 0) for value in left)/downside_sum
    if tracking:
        result['informationRatio'] = statistics.mean(excess)*252/tracking
    if beta:
        result['treynorRatio'] = mean*252/beta
    return result


def performance_rows(navs, quotes, fund_code):
    """Adjusted NAV total returns, benchmark prices, RF=0 and 252 trading days.

    Fields with unspecified proprietary definitions (VaR, attribution scores,
    Sterling/Burke/Rachev, etc.) are not silently assigned guessed formulas.
    Window boundaries must actually be covered within seven calendar days.
    """
    fund, benchmark = _series(navs), _series(quotes)
    common = sorted(set(fund) & set(benchmark))
    if not common or common[-1] != max(fund) or common[-1] != max(benchmark):
        raise ValueError('Fund and explicit benchmark endpoints differ')
    benchmark_dates = sorted(benchmark)
    end = date.fromisoformat(common[-1])
    result = {'fundCode': fund_code, 'endDate': common[-1]}
    for suffix in (*WINDOWS, 'Ytd'):
        start = cutoff(end, suffix)
        prior = [when for when in common if when <= start.isoformat()]
        if not prior or (start-date.fromisoformat(prior[-1])).days > 7:
            continue
        points = [when for when in common if when >= prior[-1]]
        gaps = [((date.fromisoformat(b)-date.fromisoformat(a)).days,
                 bisect_right(benchmark_dates, a) < bisect_right(benchmark_dates, b)-1)
                for a, b in zip(points, points[1:])]
        # Long public-market holidays are not missing NAVs. A gap over ten
        # calendar days is accepted only when the actual complete benchmark
        # source also has no intermediate quote; over twenty always refuses.
        if len(points) < 3 or any(days > 20 or (days > 10 and quoted) for days, quoted in gaps):
            continue
        for key, value in _window_metrics(points, fund, benchmark).items():
            if math.isfinite(value):
                result[key+suffix] = value
    if len(result) == 2:
        raise ValueError('No fully covered genuine return interval')
    return [{'record_key': fund_code+':'+common[-1], 'data': result}]
