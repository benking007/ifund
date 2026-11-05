"""Offline fund facts: real totals, provenance, revisions and truthful units."""
from __future__ import annotations

from pathlib import Path

import pytest

from app import db as database
from app.db.sqlite import SqliteDatabase
from app.open_data import product_collectors as collectors
from app.open_data import product_store as store
from app.open_data.product_contracts import validate_query, validate_row


@pytest.fixture
def product_db(tmp_path, monkeypatch):
    db = SqliteDatabase(str(tmp_path/'fund-products.db'))
    monkeypatch.setitem(database._STATE, 'db', db)  # pylint: disable=protected-access
    database.init_db((Path(__file__).parents[1]/'schema_sqlite.sql').read_text())
    store.migrate()
    return db


def publish(records, **changes):
    values = dict(scope_kind='fund', scope_key='110011', window_start='2026-10-01', window_end='2026-10-31',
                  source_name='issuer-a', method_version='reviewed-v1', evidence_uri='https://issuer.example/report',
                  evidence_sha256='a'*64, complete_window=True, available_fields=['date', 'title', 'fundCode'])
    values.update(changes)
    return store.ingest(223, records, **values)


def announcement(title='原始标题', key='AN1', code='110011', when='2026-10-05'):
    return {'record_key': key, 'data': {'fundCode': code, 'date': when, 'title': title}}


def query(**values):
    return store.query_product(223, {'fundCodes': ['110011'], 'beginDate': '2026-10-01',
                                   'endDate': '2026-10-31', **values})


def test_explicit_migration_and_attested_empty_window(product_db):
    assert product_db
    with pytest.raises(LookupError):
        query()
    publish([])
    assert query()['total'] == 0
    with pytest.raises(LookupError):
        query(beginDate='2026-09-30')


def test_partial_coverage_does_not_claim_an_unavailable_filter(product_db):
    assert product_db
    publish([announcement()])
    with pytest.raises(LookupError):
        query(announcementID=17)
    with pytest.raises(ValueError):
        publish([], complete_window=1)
    with pytest.raises(ValueError):
        publish([], ttl_seconds=True)


def test_revisions_filter_after_dedup_and_page_has_true_total(product_db, monkeypatch):
    assert product_db
    monkeypatch.setattr(store.time, 'time', lambda: 1000)
    publish([announcement('旧标题'), announcement('保留', 'AN2'), announcement('保留', 'AN3')])
    monkeypatch.setattr(store.time, 'time', lambda: 1001)
    publish([announcement('新标题'), announcement('保留', 'AN2'), announcement('保留', 'AN3')])
    assert query(title='旧标题')['total'] == 0
    data = query(title='保留', pageNum=2, pageSize=1)
    assert data['total'] == 2 and len(data['items']) == 1
    assert query(title='%')['total'] == 0


def test_source_authority_does_not_mix_disjoint_sources_or_methods(product_db, monkeypatch):
    assert product_db
    publish([], window_end='2026-10-15')
    publish([], window_start='2026-10-16', source_name='issuer-b')
    with pytest.raises(LookupError):
        query()
    publish([], window_start='2026-10-16', method_version='reviewed-v2')
    with pytest.raises(LookupError):
        query()
    publish([], window_start='2026-10-16')
    assert query()['total'] == 0
    monkeypatch.setattr(store.time, 'time', lambda: 2000000000)
    with pytest.raises(LookupError):
        query()


def test_batch_validation_atomic_and_same_provider_key_scoped_by_fund(product_db):
    assert product_db
    with pytest.raises(ValueError):
        publish([announcement(), announcement('错误', 'AN2', code='000001')])
    with product_db.transaction(read_only=True) as conn:
        assert conn.execute('SELECT COUNT(*) FROM open_fund_product_batch').fetchone()[0] == 0
    publish([announcement()])
    publish([announcement('另一只基金', code='000001')], scope_key='000001')
    data = store.query_product(223, {'fundCodes': ['110011', '000001'], 'beginDate': '2026-10-01',
                                   'endDate': '2026-10-31'})
    assert data['total'] == 2


@pytest.mark.parametrize('product,field,value', [(247, 'avg7dYieldAnn', True), (266, 'endShare', False),
                                               (273, 'profitCurrent', float('inf'))])
def test_financial_numbers_never_accept_booleans_or_infinity(product, field, value):
    with pytest.raises(ValueError):
        validate_row(product, {'fundCode': '110011', field: value})


def test_empty_list_and_unknown_filters_are_rejected():
    with pytest.raises(ValueError):
        validate_query(223, {'fundCodes': []})
    with pytest.raises(ValueError):
        validate_query(223, {'fundCode': '110011', 'sql': 'anything'})


def test_money_income_keeps_real_units_and_never_becomes_nav():
    data = collectors.money_rows([{'净值日期': '20261030', '每万份收益': .3556, '7日年化收益率': 1.2}], '000009')[0]['data']
    assert data == {'fundCode': '000009', 'date': '2026-10-30', 'avgDailyProfitPer10k': .3556,
                    'avg7dYieldAnn': 1.2}
    validate_row(247, data)


def test_announcements_do_not_invent_vendor_numeric_ids():
    data = collectors.announcement_rows([{'报告ID': 'AN20261105ABC', '公告日期': '2026-11-05',
                                         '公告标题': '季度报告'}], '110011')[0]
    assert data['record_key'] == 'AN20261105ABC'
    assert 'announcementId' not in data['data']
    validate_row(223, data['data'])


def test_manager_does_not_promote_birthyear_or_null_end_to_facts():
    row = collectors.manager_rows([{'ts_code': '110011.OF', 'name': '经理甲', 'begin_date': '20200101',
                                    'birthday': '1980', 'end_date': None, 'resume': '真实履历'}],
                                  '110011', '110011.OF')[0]['data']
    assert 'birthday' not in row and 'isIncumbent' not in row
    validate_row(239, row)


def test_pcf_requires_actual_publication_and_does_not_truncate_ratios():
    raw = [{'ts_code': '510300.SH', 'trade_date': '20261105', 'con_code': '600519.SH',
            'qty': 700, 'sub_flag': '必须', 'sca': 1023000.12, 'cpr': .12, 'rdr': 2}]
    unsupported_date = collectors.pcf_component_rows(raw, '510300', '510300.SH', {})[0]['data']
    assert 'publishDate' not in unsupported_date
    assert unsupported_date['stockQuantity'] == 700
    data = collectors.pcf_component_rows(raw, '510300', '510300.SH',
                                         {'2026-11-05': '2026-11-04'})[0]['data']
    assert data['publishDate'] == '2026-11-04'
    assert data['stockQuantity'] == 700 and data['fixedSubstituteAmount'] == 1023000.12
    assert 'subscribeCashSubstitutePremiumRatio' not in data
    assert data['redeemCashSubstituteDiscountRatio'] == 2
    validate_row(271, data)


def test_pcf_without_publication_date_returns_verified_cached_facts_but_date_filter_fails(product_db):
    assert product_db
    raw = [{'ts_code': '510300.SH', 'trade_date': '20261105', 'con_code': '600519.SH', 'qty': 700}]
    rows = collectors.pcf_component_rows(raw, '510300', '510300.SH')
    store.ingest(271, rows, scope_kind='fund', scope_key='510300', window_start='2026-11-05',
                 window_end='2026-11-05', source_name='tushare:etf_sh_cons', method_version='source-trade-window-v1',
                 evidence_uri='https://tushare.pro/document/2?doc_id=471', evidence_sha256='a'*64,
                 complete_window=True)
    value = store.query_product(271, {'fundCode': '510300'})
    assert value['total'] == 1 and value['items'][0]['stockQuantity'] == 700
    assert 'publishDate' not in value['items'][0]
    with pytest.raises(LookupError):
        store.query_product(271, {'fundCode': '510300', 'beginDate': '2026-11-05', 'endDate': '2026-11-05'})


def test_same_day_premium_uses_decimal_real_nav_and_refuses_stale_fill():
    from app.open_data.product_calculations import premium_rows  # pylint: disable=import-outside-toplevel
    close = [{'fundCode': '510300', 'date': '2026-11-05', 'closePrice': 1.2}]
    nav = [{'fundCode': '510300', 'date': '2026-11-05', 'unitNav': 1.0}]
    assert premium_rows(close, nav, '510300')[0]['data']['premiumRate'] == 20
    nav[0]['date'] = '2026-11-04'
    with pytest.raises(ValueError):
        premium_rows(close, nav, '510300')


def test_correlation_aligns_return_intervals_and_refuses_flat_or_short_history():
    from datetime import date, timedelta  # pylint: disable=import-outside-toplevel
    from app.open_data.product_calculations import correlation_rows  # pylint: disable=import-outside-toplevel
    nav, index, value = [], [], 10.0
    for i in range(410):
        when = (date(2025, 9, 1)+timedelta(days=i)).isoformat()
        value *= 1.003 if i % 3 == 0 else .998
        nav.append({'fundCode': '110011', 'date': when, 'adjNav': value})
        index.append({'indexCode': '000300', 'date': when, 'closePrice': value*100})
    result = correlation_rows(nav, index, '110011', '000300')[0]['data']
    assert result['corr6m'] == pytest.approx(1) and result['corr1y'] == pytest.approx(1)
    assert 'corr3y' not in result and 'corr5y' not in result
    for row in index:
        row['closePrice'] = 100
    with pytest.raises(ValueError):
        correlation_rows(nav, index, '110011', '000300')
    with pytest.raises(ValueError):
        correlation_rows(nav[-20:], index[-20:], '110011', '000300')


def test_manager_history_keeps_team_identity_and_snapshot_date_without_fake_current():
    from app.open_data.product_calculations import manager_history_rows  # pylint: disable=import-outside-toplevel
    raw = [{'fund_code': '110011', 'seq': 0, 'managers': '经理甲、经理乙', 'start_date': '2020-01-01',
            'end_date': None, 'is_current': 1, 'tenure_return': 18.5, 'fetch_time': '2026-11-05 09:01:00'}]
    row = manager_history_rows(raw, '110011')[0]['data']
    assert row['fundManagerName'] == '经理甲、经理乙' and row['date'] == '2026-11-05'
    assert row['returnTenure'] == 18.5 and 'isIncumbent' not in row
    validate_row(252, row)


def test_real_financial_sample_preserves_report_period_yuan_and_percentage():
    import json  # pylint: disable=import-outside-toplevel
    from app.open_data.product_eastmoney import financial_rows  # pylint: disable=import-outside-toplevel
    payload = json.loads((Path(__file__).parent/'fixtures/eastmoney-110011-financial-20261105.json').read_text())
    rows = financial_rows(payload, '110011')
    row = rows[0]['data']
    assert row['reportType'] == 'I' and row['reportPeriodEnd'] == '2026-06-30'
    assert row['profitCurrent'] == -2453397438.62 and row['navEnd'] == 6777001473.91
    assert row['navChangePctCurrent'] == -25.11
    assert row['navReturnWeightedAvg'] == -25.74
    assert 'profitMarginPerShareWeightedAvg' not in row
    assert 'publishDate' not in row and 'navAccAdjEnd' not in row
    for item in rows:
        validate_row(273, item['data'])
    payload['Data']['data']['FSRQ'][0] = '2026-09-30'
    with pytest.raises(ValueError):
        financial_rows(payload, '110011')


def test_disclosed_asset_ratios_use_nav_denominator_and_no_fake_review_date():
    from app.open_data.product_eastmoney import asset_rows  # pylint: disable=import-outside-toplevel
    text = '''<title>基金(110011)资产配置</title><table><tr><th>报告期</th><th>股票占净比</th>
    <th>债券占净比</th><th>现金占净比</th><th>存托凭证占净比</th><th>净资产（亿元）</th></tr>
    <tr><td>2026-09-30</td><td>87.63%</td><td>---</td><td>12.53%</td><td>---</td><td>66.84</td></tr>
    <tr><td>2026-06-30</td><td>81.34%</td><td>---</td><td>11.39%</td><td>---</td><td>67.77</td></tr></table>'''
    row = asset_rows(text, '110011')[0]
    assert row['source_report_period'] == '2026-09-30'
    assert row['data']['stockRatio'] == 87.63 and row['data']['nav'] == 6684000000
    assert 'stockValue' not in row['data'] and 'date' not in row['data'] and 'bondRatio' not in row['data']
    validate_row(259, row['data'])
    with pytest.raises(ValueError):
        asset_rows(text, '110012')
    assert asset_rows(text, '110011', latest_before='2026-06-30')[0]['data']['stockRatio'] == 81.34


def test_benchmark_only_parses_explicit_complete_weights_without_codes_or_effective_dates():
    from app.open_data.product_calculations import benchmark_rows  # pylint: disable=import-outside-toplevel
    rows = benchmark_rows('沪深300指数收益率×80%+中债总指数收益率*20%', '110011')
    assert [row['data']['benchmarkWeightPct'] for row in rows] == [80, 20]
    assert all('indexCode' not in row['data'] and 'startDate' not in row['data'] for row in rows)
    for row in rows:
        validate_row(240, row['data'])
    for text in ['沪深300指数', '沪深300指数×80%', '沪深300指数×120%+现金*-20%']:
        with pytest.raises(ValueError):
            benchmark_rows(text, '110011')


def test_corrected_publication_date_does_not_resurrect_an_older_revision(product_db, monkeypatch):
    assert product_db
    monkeypatch.setattr(store.time, 'time', lambda: 1000)
    publish([announcement(when='2026-10-05')])
    monkeypatch.setattr(store.time, 'time', lambda: 1001)
    publish([announcement(when='2026-10-06')])
    assert query(beginDate='2026-10-05', endDate='2026-10-05')['total'] == 0
    assert query(beginDate='2026-10-06', endDate='2026-10-06')['total'] == 1


def test_tushare_pagination_must_exhaust_and_uses_official_https(monkeypatch):
    import sys  # pylint: disable=import-outside-toplevel
    from types import SimpleNamespace  # pylint: disable=import-outside-toplevel
    source = SimpleNamespace(ENDPOINT='http://api.tushare.pro', call=lambda *_args: [{'id': 1}, {'id': 2}])
    monkeypatch.setitem(sys.modules, 'app.fund_nav.fetch.tushare_client', source)
    with pytest.raises(ValueError, match='ignored pagination'):
        collectors.collect_tushare('etf_sh_cons', {'ts_code': '510300.SH'}, page_size=2)
    assert source.ENDPOINT == 'https://api.tushare.pro'
    source.ENDPOINT = 'https://unreviewed-proxy.example'
    with pytest.raises(ValueError, match='official HTTPS'):
        collectors.collect_tushare('etf_sh_cons', {'ts_code': '510300.SH'})


def test_unknown_capability_and_unvalidated_metadata_cannot_be_published(product_db):
    assert product_db
    with pytest.raises(ValueError):
        publish([], available_fields=['made_up_field'])
    with pytest.raises(ValueError):
        validate_row(273, {'fundCode': '110011', 'reportType': 'F', 'reportPeriodEnd': '2026-12-31'})


def test_expired_new_covering_snapshot_never_falls_back_to_long_lived_old(product_db, monkeypatch):
    assert product_db
    monkeypatch.setattr(store.time, 'time', lambda: 1000)
    publish([announcement('旧结论')], ttl_seconds=3600)
    monkeypatch.setattr(store.time, 'time', lambda: 1010)
    publish([announcement('更正结论')], ttl_seconds=1)
    assert query()['items'][0]['title'] == '更正结论'
    monkeypatch.setattr(store.time, 'time', lambda: 1012)
    with pytest.raises(LookupError):
        query()


def test_complete_empty_new_snapshot_removes_old_fact_and_does_not_resurrect_after_expiry(product_db, monkeypatch):
    assert product_db
    monkeypatch.setattr(store.time, 'time', lambda: 1000)
    publish([announcement('已删除')], ttl_seconds=3600)
    monkeypatch.setattr(store.time, 'time', lambda: 1010)
    publish([], ttl_seconds=1)
    assert query()['total'] == 0
    monkeypatch.setattr(store.time, 'time', lambda: 1012)
    with pytest.raises(LookupError):
        query()


def test_latest_covering_capability_does_not_fall_back_to_older_version(product_db, monkeypatch):
    assert product_db
    monkeypatch.setattr(store.time, 'time', lambda: 1000)
    old = announcement()
    old['data']['announcementId'] = 17
    publish([old])
    monkeypatch.setattr(store.time, 'time', lambda: 1010)
    publish([announcement('新快照无此字段')])
    with pytest.raises(LookupError):
        query(announcementID=17)
