"""Complete catalog facts, real THS/PDF weights and failure-not-empty boundaries."""
from __future__ import annotations

import copy
from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Blueprint, Flask

from app import db as database
from app.db.sqlite import SqliteDatabase
from app.open_data import product_concepts as concepts, product_store as store, product_reports as reports
from app.open_data import product_manager_history as history
from app.open_data.product_routes import register_product_routes

FIXTURES = Path(__file__).parent/'fixtures'


@pytest.fixture
def source(tmp_path, monkeypatch):
    db = SqliteDatabase(str(tmp_path/'catalog.db'))
    monkeypatch.setitem(database._STATE, 'db', db)  # pylint: disable=protected-access
    database.init_db((Path(__file__).parents[1]/'schema_sqlite.sql').read_text())
    store.migrate()
    database.insert('fund_basic_ext', {'fund_code': '110011', 'ts_code': '110011.OF',
                                       'status': 'L', 'source': 'tushare'})
    application = Flask(__name__)
    bp = Blueprint('products', __name__, url_prefix='/api/open-data/v1')
    register_product_routes(bp)
    application.register_blueprint(bp)
    return application.test_client()


@pytest.fixture(scope='module')
def actual_exposure():
    metadata = json.loads((FIXTURES/'issuer-110011-2026H1.json').read_text())
    report = reports.parse_document((FIXTURES/'issuer-110011-2026H1.pdf').read_bytes(), metadata,
                                    '110011', periodic=True)
    body = json.loads((FIXTURES/'ths-full-portfolio-110011-20261106.json').read_text())
    names = {row['conceptCode']: row['conceptName'] for row in body['directory']['items']}
    stocks = reports.full_stock_rows(report)
    return concepts.exposure_rows(stocks, body['responses'], names, '110011'), names, stocks, body


def test_actual_full_portfolio_intersection_has_exact_weight_and_no_foreign_or_non_N_guess(actual_exposure):
    rows, names, stocks, body = actual_exposure
    assert len(stocks) == 64 and len(names) == 395
    assert sum(len(part['items']) for part in body['responses']) == 808
    assert all(row['data']['conceptCode'] in names for row in rows)
    code = next(row['data']['conceptCode'] for row in rows)
    constituents = {row['stockCode'] for part in body['responses'] for row in part['items']
                    if row['conceptCode'] == code}
    expected = sum(stock['nav_ratio'] for stock in stocks if stock['stock_code'] in constituents)
    assert rows[0]['data']['holdingWeightLatest'] == pytest.approx(expected)
    assert max(row['data']['holdingWeightLatest'] for row in rows) <= 43.84
    malformed = copy.deepcopy(body['responses'])
    malformed[0]['subjectSnapshots'][0]['conceptRowCount'] += 1
    with pytest.raises(ValueError, match='row count'):
        concepts.exposure_rows(stocks, malformed, names, '110011')
    with pytest.raises(ValueError, match='coverage'):
        concepts.exposure_rows(stocks, body['responses'][:-1], names, '110011')


def _publish_concept(rows):
    for row in rows:
        row['is_etf'] = False
    store.ingest(263, rows, scope_kind='fund', scope_key='110011',
                 window_start='2026-06-30', window_end='2026-06-30', source_name='issuer+ths',
                 method_version=concepts.METHOD, evidence_uri='https://pdf.dfcfw.com/',
                 evidence_sha256='a'*64, complete_window=True)


def test_concept_global_generation_exact_pages_zero_and_physical_integrity(source, actual_exposure, monkeypatch):
    rows, names, _, _ = actual_exposure
    _publish_concept(copy.deepcopy(rows))
    monkeypatch.setattr(concepts, 'dictionary', lambda: names)
    result = concepts.materialize(SimpleNamespace())
    assert result['quality']['row_count'] == len(rows)
    code = rows[0]['data']['conceptCode']
    query = {'conceptCode': code, 'pageSize': 1, 'pageNum': 1}
    response = source.get('/api/open-data/v1/fund-products/263', query_string={'query_json': json.dumps(query)})
    assert response.status_code == 200, response.text
    assert response.json['total'] == 1 and response.json['items'][0] == rows[0]['data']
    absent = next(code for code in names if code not in {r['data']['conceptCode'] for r in rows})
    assert concepts.query(263, {'conceptCode': absent})['total'] == 0
    with pytest.raises(ValueError, match='CLS'):
        concepts.query(263, {'conceptCode': 'C01'})
    with pytest.raises(LookupError, match='quarterly'):
        concepts.query(267, {'conceptCodes': [code], 'matchType': 2, 'threshold': 0})
    with database.get_db().transaction() as conn:
        store._sql(conn, 'DELETE FROM open_fund_concept_fact WHERE generation_id=? AND concept_code=?',
                   (result['generation'], code))  # pylint: disable=protected-access
    assert source.get('/api/open-data/v1/fund-products/263',
                      query_string={'query_json': json.dumps(query)}).status_code == 503


def test_catalog_addition_never_looks_like_zero_exposure(source, actual_exposure, monkeypatch):
    rows, names, _, _ = actual_exposure
    _publish_concept(copy.deepcopy(rows))
    monkeypatch.setattr(concepts, 'dictionary', lambda: names)
    concepts.materialize(SimpleNamespace())
    database.insert('fund_basic_ext', {'fund_code': '006581', 'ts_code': '006581.OF',
                                       'status': 'L', 'source': 'tushare'})
    with pytest.raises(LookupError, match='uncovered'):
        concepts.query(263, {'conceptCode': rows[0]['data']['conceptCode']})
    with pytest.raises(ValueError, match='catalog member'):
        concepts.materialize(SimpleNamespace())


def test_actual_manager_teams_global_population_filtered_after_full_manifest(source):
    rows = history.history_rows((FIXTURES/'manager-history-110011-20261106.html').read_text(), '110011')
    today = date.today().isoformat()
    store.ingest(252, rows, scope_kind='fund', scope_key='110011', window_start=today, window_end=today,
                 source_name='eastmoney:manager-team-tenures', method_version=history.METHOD,
                 evidence_uri='https://fundf10.eastmoney.com/', evidence_sha256='b'*64,
                 complete_window=True)
    history.materialize(SimpleNamespace())
    body = history.query({'fundManagerName': '张坤', 'pageSize': 1, 'pageNum': 2})
    assert body['total'] == 2 and len(body['items']) == 1
    assert all('date' not in row for row in body['items'])
    database.insert('fund_basic_ext', {'fund_code': '006581', 'ts_code': '006581.OF',
                                       'status': 'L', 'source': 'tushare'})
    with pytest.raises(LookupError, match='catalog generation'):
        history.query({})


def test_complete_purchase_index_filters_funds_and_retains_latest_authority(source):
    today = date.today().isoformat()
    for index in range(4):
        store.ingest(238, [{'record_key': code, 'data': {'fundCode': code, 'subscribeLimit': index+1}}
                           for code in ['110011', '006581']], scope_kind='global', scope_key='*',
                     window_start=today, window_end=today, source_name='complete-purchase-index',
                     method_version='v2', evidence_uri='https://fund.eastmoney.com/', evidence_sha256='c'*64,
                     complete_window=True)
    assert store.query_product(238, {'fundCode': '110011'})['items'] == [
        {'fundCode': '110011', 'subscribeLimit': 4}]
    assert store.query_product(238, {'fundCodes': ['110011', '006581'], 'pageNum': 2, 'pageSize': 1})['total'] == 2
    assert store.prune_purchase_snapshots() == 2
    assert store.query_product(238, {'fundCode': '006581'})['items'][0]['subscribeLimit'] == 4
    with pytest.raises(LookupError, match='universe'):
        store.query_product(238, {'fundCode': '999999'})
    assert source


def test_current_fee_tiers_do_not_switch_to_amendment_events_by_job_completion_order(source):
    today = date.today().isoformat()
    store.ingest(243, [{'record_key': 'tier', 'data': {'fundCode': '110011', 'chargeRateMin': 1.5,
                                                   'chargeCategoryDesc': '申购费率'}}],
                 scope_kind='fund', scope_key='110011', window_start=today, window_end=today,
                 source_name='eastmoney:disclosed-fund', method_version='em-disclosed-original-fee-tiers-v1',
                 evidence_uri='https://fundf10.eastmoney.com/', evidence_sha256='a'*64, complete_window=True)
    store.ingest(243, [{'record_key': 'amendment', 'data': {'fundCode': '110011', 'publishDate': today,
                                                        'chargeRateMin': 1.2, 'chargeCategoryDesc': '管理费率'}}],
                 scope_kind='fund', scope_key='110011', window_start=today, window_end=today,
                 source_name='issuer:fee-amendments', method_version='actual-amendment-v1',
                 evidence_uri='https://pdf.dfcfw.com/', evidence_sha256='b'*64, complete_window=True)
    assert store.query_product(243, {'fundCode': '110011'})['items'][0]['chargeRateMin'] == 1.5
    assert store.query_product(243, {'fundCode': '110011', 'beginDate': today})['items'][0]['chargeRateMin'] == 1.2
    assert source


def test_real_new_fund_has_no_completed_report_period_and_is_not_zero_exposure(source, actual_exposure, monkeypatch):
    from app.open_data.product_outcomes import NotApplicableError  # pylint: disable=import-outside-toplevel
    real = json.loads((FIXTURES/'new-fund-no-periodic-disclosure-20261106.json').read_text())
    master = real['master']; code = master['ts_code'].split('.')[0]
    assert master['found_date'] == '20261106'
    assert real['periodic_index']['ErrCode'] == -999  # Failed transport is NOT a successful empty index.
    database.insert('fund_basic_ext', {**master, 'fund_code': code, 'source': 'tushare'})
    def must_not_fetch(_code):
        raise AssertionError('Inception proves this completed report cannot exist; do not infer from failed source')
    monkeypatch.setattr(reports, 'report_index', must_not_fetch)
    with pytest.raises(NotApplicableError) as error:
        concepts.collect_scope(SimpleNamespace(fund_code=code, end='2026-11-06'))
    assert error.value.evidence['latest_completed_report_period'] == '2026-06-30'
    rows, names, _, _ = actual_exposure
    _publish_concept(copy.deepcopy(rows))
    monkeypatch.setattr(concepts, 'dictionary', lambda: names)
    output = concepts.materialize(SimpleNamespace())
    assert output['quality']['dataset_scope'] == {'catalog_fund_count': 2, 'eligible_fund_count': 1,
                                               'undisclosed_fund_count': 0, 'not_applicable_fund_count': 1}
    body = concepts.query(263, {'conceptCode': rows[0]['data']['conceptCode']})
    assert body['eligible_fund_count'] == 1 and body['not_applicable_fund_count'] == 1
    assert body['total'] == 1 and body['items'][0]['fundCode'] == '110011'
    assert source


def test_non_disclosure_evidence_is_hash_bound_not_a_fact_or_old_version_fallback(source):
    proof = {'fund_code': '110011', 'source_rows': 0, 'index': [], 'index_sha256': 'a'*64}
    first = store.record_disclosure_state(263, '110011', 'full-verified-index', proof)
    second = store.record_disclosure_state(263, '110011', 'full-verified-index', proof)
    assert set(second) == {'version_id', 'observed_at'}
    with database.get_db().transaction() as conn:
        store._sql(conn, 'UPDATE open_fund_disclosure_eligibility SET evidence_sha256=? WHERE version_id=?',
                   ('0'*64, second['version_id']))  # pylint: disable=protected-access
    with database.get_db().transaction(read_only=True) as conn:
        with pytest.raises(LookupError, match='corrupted'):
            store.disclosure_state(conn, 263, '110011')
        assert store._sql(conn, 'SELECT COUNT(*) AS n FROM open_fund_product_fact', fetch=True)[0]['n'] == 0
    assert first['version_id'] != second['version_id'] and source


def test_all_unreported_catalog_is_not_a_verified_empty_financial_product(source, monkeypatch):
    names = {'885001.TI': 'verified-current-concept'}
    proof = {'fund_code': '110011', 'source_rows': 0, 'index': [], 'sha256': 'a'*64}
    store.record_disclosure_state(263, '110011', 'full-verified-index', proof)
    monkeypatch.setattr(concepts, 'dictionary', lambda: names)
    output = concepts.materialize(SimpleNamespace())
    assert output['quality']['status'] == 'not_disclosed'
    assert output['quality']['complete'] is True and output['quality']['verified_empty'] is False
    assert output['quality']['evidence']['undisclosed_funds'] == 1
    with pytest.raises(LookupError, match='No catalog fund'):
        concepts.query(263, {'conceptCode': '885001.TI'})
    assert source


def test_industry_reverse_declares_actual_new_fund_exclusion_separately_from_verified_zero(source):
    from app.open_data import product_additional as additional  # pylint: disable=import-outside-toplevel
    from app.open_data.product_outcomes import NotApplicableError  # pylint: disable=import-outside-toplevel
    real = json.loads((FIXTURES/'new-fund-no-periodic-disclosure-20261106.json').read_text())
    master = real['master']; code = master['ts_code'].split('.')[0]
    database.insert('fund_basic_ext', {**master, 'fund_code': code, 'source': 'tushare'})
    with pytest.raises(NotApplicableError):
        additional.ingest_collection(SimpleNamespace(kind='industry-exposure', fund_code=code,
                                                     end='2026-11-06', start='0001-01-01'))
    payload = json.loads((FIXTURES/'additional-industry-20261106.json').read_text())
    rows = additional.industry_rows(payload, '110011')
    store.ingest(264, rows, scope_kind='fund', scope_key='110011', window_start='2026-11-06', window_end='2026-11-06',
                 source_name='actual-industry-source', method_version='em-native-industry-nav-current-v1:real',
                 evidence_uri='https://api.fund.eastmoney.com/f10/HYPZ/', evidence_sha256='a'*64, complete_window=True)
    result = additional._industry_reverse(SimpleNamespace(product=262))
    assert result['quality']['dataset_scope'] == {'catalog_fund_count': 2, 'eligible_fund_count': 1,
                                                'undisclosed_fund_count': 0, 'not_applicable_fund_count': 1}
    body = store.query_product(262, {'industryCode': rows[0]['data']['industryCode']})
    assert body['scope_metadata'][0]['not_applicable_fund_count'] == 1
    assert 'valid_until' not in body and 'valid_until' not in result['quality']
    assert source
