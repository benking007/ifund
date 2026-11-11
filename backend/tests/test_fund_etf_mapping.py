"""Actual annual ETF asset denominator and strict current constituent identity."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from app.open_data import product_etf_mapping as mapping, product_concepts as concepts
from app.open_data.product_contracts import validate_query
from app.open_data.product_reports import parse_document


@pytest.fixture(scope='module')
def report():
    fixtures = Path(__file__).parent/'fixtures'
    return parse_document((fixtures/'issuer-510300-2025FY.pdf').read_bytes(),
                          json.loads((fixtures/'issuer-510300-2025FY.json').read_text()), '510300', periodic=True)


def test_actual_full_annual_stock_table_and_gross_assets(report):
    stocks = mapping.complete_domestic_stocks(report)
    assert len(stocks) == 342
    assert sum(row['market_value'] for row in stocks) == pytest.approx(416562848300.30)
    assert sum(row['nav_ratio'] for row in stocks) == pytest.approx(98.66)
    assert mapping.gross_assets(report) == pytest.approx(422462037128.46)
    assert report['period'] == '2025-12-31' and report['published'] == '2026-03-31'


def test_annual_industry_asset_weight_is_not_nav_weight(report):
    stocks = mapping.complete_domestic_stocks(report)
    body = {'source': 'open_market_products', 'product': 'stock_industry', 'total': len(stocks),
            'items': [{'stockCode': row['stock_code'], 'industryCodeSwL1': 'test-only-one-bucket',
                       'industryNameSwL1': 'isolated-formula-bucket'} for row in stocks]}
    row = mapping.industry_rows(report, stocks, [body], '510300', 'actualETF')[0]['data']
    assert row['holdingAssetAnnual'] == pytest.approx(416562848300.30/422462037128.46*100)
    assert row['holdingNavAnnual'] == pytest.approx(98.66)
    assert row['holdingAssetAnnual'] != row['holdingNavAnnual']
    assert 'holdingAssetLatest' not in row and 'holdingAssetQuarterly' not in row
    with pytest.raises(ValueError, match='All disclosed'):
        mapping.industry_rows(report, stocks, [{**body, 'total': len(stocks)-1, 'items': body['items'][:-1]}],
                              '510300', 'actualETF')


def test_missing_all_stock_ordinal_or_boundary_fails_closed(report):
    corrupted = {**report, 'text': report['text'].replace('43,712,032', 'missing-number', 1)}
    with pytest.raises(ValueError):
        mapping.complete_domestic_stocks(corrupted)
    with pytest.raises(ValueError):
        mapping.complete_domestic_stocks({**report, 'text': report['text'].split('8.3 期末')[0]})


def test_association_selectors_xor_and_real_annual_threshold():
    for product, selector in ((142, 'industryCodes'), (143, 'conceptCodes')):
        validate_query(product, {'fundCodes': ['510300'], 'minAnnualWeight': 0})
        with pytest.raises(ValueError, match='exactly one'):
            validate_query(product, {'fundCodes': ['510300'], selector: ['test']})
        with pytest.raises(ValueError):
            validate_query(product, {'fundCodes': ['510300'], 'minAnnualWeight': 100.1})


def test_cls_native_memberships_cannot_be_substituted_by_ths():
    stocks = [{'stock_code': '600519', 'nav_ratio': 3.37}]
    body = {'source': 'cls_market_products', 'product': 'memberships', 'conceptSource': 'CLS',
            'completeSubjectSnapshots': True, 'historicalPointInTime': False,
            'requestedSymbols': ['600519.SH'], 'coveredSymbols': ['600519.SH'], 'total': 1,
            'subjectSnapshots': [{'symbol': '600519.SH', 'rowCount': 1, 'snapshotId': 'isolated',
                                  'observedAt': datetime.now(timezone.utc).isoformat()}],
            'items': [{'stockCode': '600519', 'conceptCode': 'CLS80014', 'conceptName': '白酒'}]}
    rows = concepts.exposure_rows(stocks, [body], {'CLS80014': '白酒'}, '510300',
                                 source_identity=('cls_market_products', 'memberships', 'CLS'))
    assert rows[0]['data']['holdingWeightAnnual'] == 3.37
    with pytest.raises(ValueError):
        concepts.exposure_rows(stocks, [{**body, 'conceptSource': 'THS'}], {'CLS80014': '白酒'}, '510300',
                               source_identity=('cls_market_products', 'memberships', 'CLS'))


def test_membership_observation_needs_time_zone_and_is_not_in_future():
    import time  # pylint: disable=import-outside-toplevel
    now = time.time()
    old = {'subjectSnapshots': [{'observedAt': datetime.fromtimestamp(now-30*86400, timezone.utc).isoformat()}]}
    concepts.check_observed([old])
    future = {'subjectSnapshots': [{'observedAt': datetime.fromtimestamp(now+3600, timezone.utc).isoformat()}]}
    with pytest.raises(ValueError, match='future'):
        concepts.check_observed([future])
    naive = {'subjectSnapshots': [{'observedAt': '2026-11-06T00:00:00'}]}
    with pytest.raises(ValueError, match='time zone'):
        concepts.check_observed([naive])
