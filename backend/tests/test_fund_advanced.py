"""Real issuer documents and exact fee/risk/role boundaries, isolated only."""
from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path

import pytest

from app.open_data import product_fees as fees, product_reports as reports, product_performance as performance
from app.open_data.product_contracts import validate_row

FIXTURES = Path(__file__).parent/'fixtures'


@pytest.fixture(scope='module', params=['110011', '006581'])
def report(request):
    code = request.param
    metadata = json.loads((FIXTURES/('issuer-'+code+'-2026Q3.json')).read_text())
    document = reports.parse_document((FIXTURES/('issuer-'+code+'-2026Q3.pdf')).read_bytes(), metadata, code)
    return code, document


def test_actual_quarter_profit_uses_exact_report_and_class_column(report):
    code, document = report
    rows = reports.financial_rows(document, code)
    value = validate_row(274, rows[0]['data'])
    assert value['reportPeriodEnd'] == '2026-09-30' and value['reportType'] == 'Q3'
    expected = {'110011': (51926427.85, 4.0054, '2026-10-27'),
                '006581': (-8649216.38, 1.0563, '2026-10-26')}
    assert (value['profitCurrent'], value['navPerShareEnd'], value['publishDate']) == expected[code]
    if code == '006581':
        other = reports.financial_rows(document, '017257')[0]['data']
        assert other['profitCurrent'] == -906804.2 and other['navPerShareEnd'] == 1.0668


def test_real_fof_and_same_level_class_relations(report):
    code, document = report
    if code == '110011':
        with pytest.raises(ValueError):
            reports.fof_rows(document, code)
        with pytest.raises(ValueError):
            reports.relationship_rows(document, code)
        return
    related = reports.relationship_rows(document, code)[0]['data']
    assert related == {'fundCode': code, 'relatedFundCode': '017257', 'relationType': 10, 'endDate': '2026-09-30'}
    rows = reports.fof_rows(document, code)
    assert len(rows) == 10
    assert rows[0]['data']['holdingFundCode'] == '968052'
    assert rows[0]['data']['holdingShares'] == 3433038.87
    assert rows[0]['data']['holdingValue'] == 36115568.91
    for row in rows:
        validate_row(260, row['data'])


def test_actual_share_flows_are_not_derived_from_net_change(report):
    code, document = report
    value = validate_row(266, reports.share_rows(document, code)[0]['data'])
    assert value['subscribeShare'] > 0 and value['redeemShare'] > 0
    assert value['beginShare']+value['subscribeShare']-value['redeemShare'] == pytest.approx(value['endShare'])


def test_error_page_cannot_masquerade_as_a_pdf():
    with pytest.raises(ValueError, match='genuine PDF'):
        reports.parse_document(b'<html>error</html>', {}, '110011')


def test_original_fee_tiers_and_cash_fee_units():
    rows = fees.fee_rows((FIXTURES/'fee-tiers-110011-20261106.html').read_text(), '110011')
    assert len(rows) == 12
    for row in rows:
        validate_row(243, row['data'])
    subscription = [x['data'] for x in rows if x['data']['chargeCategoryDesc'] == '申购费率']
    assert subscription[0]['chargeRateMin'] == 1.5  # not the channel promotion 0.15
    assert subscription[-1]['chargeUnit'] == 7 and subscription[-1]['chargeRateMin'] == 1000
    assert subscription[1]['chargeCriteriaRangeStart1'] == 100
    assert subscription[1]['chargeCriteriaRangeEnd1'] == 500
    assert subscription[1]['isIncludeRangeStart1'] == 2
    with pytest.raises(ValueError, match='identity'):
        fees.fee_rows((FIXTURES/'fee-tiers-110011-20261106.html').read_text(), '999999')


def test_risk_metrics_use_real_windows_and_zero_denominators_stay_absent():
    points = [{'date': (date(2024, 1, 1)+timedelta(days=i)).isoformat(), 'value': 1+i/10000}
              for i in range(1050) if (date(2024, 1, 1)+timedelta(days=i)).weekday() < 5]
    result = performance.performance_rows(points, points, '110011')[0]['data']
    validate_row(253, result)
    assert result['beta1y'] == pytest.approx(1)
    assert result['trackingError1y'] == 0
    assert 'informationRatio1y' not in result
    assert 'return5y' not in result and 'returnSinceInception' not in result
    assert result['maxDrawdown1y'] == 0
    assert 'calmarRatio1y' not in result
    with pytest.raises(ValueError, match='endpoints'):
        performance.performance_rows(points, points[:-1], '110011')


def test_award_organizer_list_is_not_a_star_rating_or_guessed_code():
    from app.open_data.product_awards import SOURCES, award_rows  # pylint: disable=import-outside-toplevel
    document = (FIXTURES/'awards-organizer-22nd-2025.html').read_text()
    profile = (FIXTURES/'issuer-profile-110029-20261106.html').read_text()
    rows = award_rows(document, profile, '110029', SOURCES[0])
    assert len(rows) == 1
    value = validate_row(242, rows[0]['data'])
    assert value['awardOrgName'] == '中国证券报'
    assert value['awardName'].endswith('七年期开放式混合型持续优胜金牛基金')
    assert value['publishDate'] == '2025-12-31'
    assert 'awardCode' not in value and 'awardOrgCode' not in value and 'awardYear' not in value
    with pytest.raises(ValueError, match='28'):
        award_rows(document.replace('海富通国策导向混合', ''), profile, '110029', SOURCES[0])


def test_actual_half_year_holder_full_shares_and_employee_role():
    metadata = json.loads((FIXTURES/'issuer-110011-2026H1.json').read_text())
    document = reports.parse_document((FIXTURES/'issuer-110011-2026H1.pdf').read_bytes(), metadata,
                                      '110011', periodic=True)
    value = validate_row(265, reports.holder_rows(document, '110011')[0]['data'])
    assert value['holderCount'] == 1341053
    assert value['instHoldingShare'] == 7378020.58
    assert value['retailHoldingShare'] == 1697103626.43
    assert value['staffHoldingShare'] == 2942978.1
    assert value['staffHoldingRatio'] == .1727
    assert value['reportDate'] == '2026-06-30' and value['publishDate'] == '2026-08-31'
    stocks = reports.full_stock_rows(document)
    assert len(stocks) == 64 and sum(row['nav_ratio'] for row in stocks) == pytest.approx(43.84)
    assert next(row for row in stocks if row['stock_code'] == '600519')['symbol'] == '600519.SH'


def test_actual_fee_amendment_has_real_publication_and_execution_dates():
    from app.open_data.product_fee_history import fee_change_rows  # pylint: disable=import-outside-toplevel
    profile = (FIXTURES/'issuer-profile-110011-20261106.html').read_text()
    metadata = {'FUNDCODE': '110011', 'PUBLISHDATE': '2023-07-08'}
    rows = fee_change_rows((FIXTURES/'issuer-fee-change-20230708.pdf').read_bytes(), profile, metadata, '110011')
    assert len(rows) == 2
    values = [validate_row(243, row['data']) for row in rows]
    assert {row['chargeRateMin'] for row in values} == {1.2, .2}
    assert all(row['publishDate'] == '2023-07-08' and row['executeDate'] == '2023-07-10' for row in values)
    with pytest.raises(ValueError, match='identity'):
        fee_change_rows((FIXTURES/'issuer-fee-change-20230708.pdf').read_bytes(), profile, metadata, '999999')


def test_actual_manager_history_preserves_joint_teams_and_does_not_invent_publication():
    from app.open_data.product_manager_history import history_rows  # pylint: disable=import-outside-toplevel
    rows = history_rows((FIXTURES/'manager-history-110011-20261106.html').read_text(), '110011')
    assert len(rows) == 3
    values = [validate_row(252, row['data']) for row in rows]
    assert values[0]['fundManagerName'] == '张坤 彭珂'
    assert values[0]['isIncumbent'] == 1 and 'tenureEndDate' not in values[0]
    assert values[1]['tenureStartDate'] == '2012-09-28'
    assert values[1]['tenureEndDate'] == '2026-06-26'
    assert values[1]['returnTenure'] == 288.98
    assert all('date' not in row and 'fundManagerCode' not in row for row in values)


def test_actual_official_adjusted_nav_and_explicit_benchmark_risk_calculations():
    raw = json.loads((FIXTURES/'performance-110011-hs300-20261106.json').read_text())
    navs = [{'date': row['nav_date'], 'value': row['adj_nav']} for row in raw['fund_nav']]
    quotes = [{'date': row['trade_date'], 'value': row['close']} for row in raw['index_daily']]
    result = performance.performance_rows(navs, quotes, '110011')[0]['data']
    validate_row(253, result)
    assert result['endDate'] == '2026-11-05'
    assert len(result) >= 80
    assert result['trackingError1y'] >= 0 and -2 < result['beta1y'] < 3
    # This is an explicitly chosen test index, not a claimed official benchmark
    # for the QDII product's multi-component benchmark.
    assert raw['benchmark_role'].startswith('explicit_test_benchmark')


def test_immutable_report_cache_avoids_reparse_and_rejects_corrupt_payload(tmp_path, monkeypatch):
    from app.open_data.product_report_cache import load_report  # pylint: disable=import-outside-toplevel
    from curl_cffi import requests  # pylint: disable=import-outside-toplevel
    metadata = json.loads((FIXTURES/'issuer-110011-2026Q3.json').read_text())
    content = (FIXTURES/'issuer-110011-2026Q3.pdf').read_bytes()
    calls = []
    class Response:
        def __init__(self):
            self.content = content
        def raise_for_status(self):
            return None
    monkeypatch.setenv('IFUND_OPEN_REPORT_CACHE_DIR', str(tmp_path))
    monkeypatch.setattr(requests, 'get', lambda *a, **k: (calls.append(1), Response())[1])
    first = load_report(metadata, '110011', parser=reports.parse_document)
    assert load_report(metadata, '110011', parser=reports.parse_document) == first and len(calls) == 1
    next(tmp_path.glob('*.json')).write_text('{broken')
    assert load_report(metadata, '110011', parser=reports.parse_document) == first and len(calls) == 2


def test_real_benchmark_holiday_is_not_misclassified_as_a_missing_fund_observation():
    points = [{'date': (date(2025, 1, 1)+timedelta(days=i)).isoformat(), 'value': 1+i/10000}
              for i in range(700) if (date(2025, 1, 1)+timedelta(days=i)).weekday() < 5]
    closed = [row for row in points if not '2026-02-13' < row['date'] < '2026-02-25']
    result = performance.performance_rows(closed, closed, '110011')[0]['data']
    assert 'return1y' in result
    missing_nav = performance.performance_rows(closed, points, '110011')[0]['data']
    assert 'return1y' not in missing_nav


def test_old_period_revision_does_not_displace_newer_report_period():
    rows = [{'TITLE': '2025年第4季度报告更正', 'PUBLISHDATE': '2026-11-01', 'ID': 'older-period-new-revision'},
            {'TITLE': '2026年第3季度报告', 'PUBLISHDATE': '2026-10-27', 'ID': 'new-period'},
            {'TITLE': '2026年第3季度报告修订', 'PUBLISHDATE': '2026-10-28', 'ID': 'new-period-revised'}]
    versions = reports.latest_period_versions(rows)
    assert len(versions) == 2 and versions[-1]['ID'] == 'new-period-revised'
    with pytest.raises(ValueError):
        reports.latest_period_versions(rows+[dict(rows[-1], ID='unknown-order')])


def test_report_summary_is_not_a_full_report_or_a_revision_conflict():
    rows = [{'TITLE': '2020年年度报告摘要', 'PUBLISHDATE': '2021-03-31', 'ID': 'summary'},
            {'TITLE': '2020年年度报告', 'PUBLISHDATE': '2021-03-31', 'ID': 'full'},
            {'TITLE': '2025年年度报告', 'PUBLISHDATE': '2026-03-31', 'ID': 'latest'}]
    assert reports.latest_period_versions(rows, periodic=True)[0]['ID'] == 'full'
    assert reports.latest_period_versions(rows, periodic=True, latest_only=True)[0]['ID'] == 'latest'
