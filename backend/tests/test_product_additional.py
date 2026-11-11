"""Public observations through offline publication and strict cached reads."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from flask import Blueprint, Flask
import pytest

from app import db as database
from app.db.sqlite import SqliteDatabase
from app.open_data import product_additional as additional
from app.open_data import product_store as store
from app.open_data.product_contracts import validate_row
from app.open_data.product_routes import register_product_routes

FIXTURES = Path(__file__).parent/'fixtures'


def raw(name):
    """Read an exact public source extract, with source date in its filename."""
    return (FIXTURES/name).read_text(encoding='utf-8')


def observations():
    """Map only the verified fields from actual public observations."""
    company = json.loads(raw('additional-company-20261106.json'))
    quarter = json.loads(raw('additional-quarter_nav-20261106.json'))
    return {
        238: additional.purchase_rows(raw('additional-purchase-subset-20261106.js'), '110011'),
        241: additional.investment_rows(raw('additional-profile-extract-20261106.html'), '110011'),
        244: additional.company_rows(company['master'], company['company']),
        248: additional.peer_rows(raw('additional-peer-curves-20261106.js'), '110011'),
        251: additional.manager_rows(raw('additional-manager-profile-20261106.html'),
                                    json.loads(raw('additional-manager_curve-20261106.json')), '30189744', '张坤'),
        264: additional.industry_rows(json.loads(raw('additional-industry-20261106.json')), '110011'),
        265: additional.holder_rows(raw('additional-holder-20261106.js'), '110011'),
        270: additional.pcf_rows(json.loads(raw('additional-pcf-20261106.json')), '510300'),
        274: additional.quarter_nav_rows(quarter['raw'], '110011', '110011.OF'),
    }


@pytest.fixture
def source(tmp_path, monkeypatch):
    """A complete isolated source application, with no upstream calls on GET."""
    db = SqliteDatabase(str(tmp_path/'additional.db'))
    monkeypatch.setitem(database._STATE, 'db', db)  # pylint: disable=protected-access
    database.init_db((Path(__file__).parents[1]/'schema_sqlite.sql').read_text())
    store.migrate()
    directory = json.loads(raw('additional-complete-manager-directory-20261106.json'))
    store.publish_manager_directory(directory, len(directory), 'b'*64)
    company = json.loads(raw('additional-company-20261106.json'))
    database.insert('fund_company', company['company'])
    database.insert('fund_basic_ext', {**company['master'], 'status': 'L'})
    app = Flask(__name__)
    blueprint = Blueprint('products', __name__, url_prefix='/api/open-data/v1')
    register_product_routes(blueprint)
    app.register_blueprint(blueprint)
    return app.test_client(), db


def publish(product, records):
    """Publish an exact snapshot or quarter window under its true source scope."""
    scope, key = ('manager', '张坤') if product == 251 else ('fund', '510300' if product == 270 else '110011')
    window = ('2026-09-30', '2026-09-30') if product == 274 else ('2026-11-06', '2026-11-06')
    return store.ingest(product, records, scope_kind=scope, scope_key=key,
                        window_start=window[0], window_end=window[1], source_name='eastmoney:manager:30189744' if product == 251 else 'public-source-observations',
                        method_version='reviewed-additional-source-v1', evidence_uri='https://fundf10.eastmoney.com',
                        evidence_sha256='a'*64, complete_window=True)


@pytest.mark.parametrize('product', [238, 241, 244, 248, 251, 264, 265, 270, 274])
def test_real_observations_roundtrip_through_source_http(source, product):
    http, _ = source
    records = observations()[product]
    for record in records:
        validate_row(product, record['data'])
    publish(product, records)
    query = {'fundManagerName': '张坤'} if product == 251 else {'fundCode': '510300' if product == 270 else '110011'}
    result = http.get('/api/open-data/v1/fund-products/'+str(product), query_string={'query_json': json.dumps(query)})
    assert result.status_code == 200, result.text
    body = result.json
    assert body['total'] == len(records) and body['source'] == 'ifund_products'
    assert sorted(body['items'], key=lambda row: json.dumps(row, sort_keys=True)) == sorted(
        [record['data'] for record in records], key=lambda row: json.dumps(row, sort_keys=True))


def test_current_snapshot_dates_are_not_fabricated_or_filterable(source):
    http, _ = source
    for product in [238, 265, 270]:
        rows = observations()[product]
        publish(product, rows)
        assert not any('resumeDate' in row['data'] or 'publishDate' in row['data'] or 'reportDate' in row['data']
                       for row in rows)
        query = {'fundCode': '510300' if product == 270 else '110011', 'beginDate': '2026-11-01'}
        assert http.get('/api/open-data/v1/fund-products/'+str(product),
                        query_string={'query_json': json.dumps(query)}).status_code == 503


def test_source_units_and_non_equivalent_roles_are_preserved():
    values = observations()
    assert values[238][0]['data']['subscribeLimit'] == 50000
    assert '限大额' in values[238][0]['data']['remark']
    holder = values[265][0]['data']
    assert holder['instHoldingRatio'] == .43 and holder['retailHoldingRatio'] == 99.57
    assert 'staffHoldingRatio' not in holder and 'instHoldingShare' not in holder
    header = values[270][0]['data']
    assert header['creationUnit'] == 900000 and header['estimatedCashComponent'] == 94551.55
    assert header['cashBalance'] == 95477.55 and header['prevTradeDate'] == '2026-11-04'
    assert header['nav'] == 4.1785 and header['navPerCreationUnit'] == 3760682.55
    assert 'industryStandardCode' not in values[264][0]['data']
    assert values[274][0]['data']['reportPeriodEnd'] == '2026-09-30'
    assert 'profitCurrent' not in values[274][0]['data']


def test_literal_decoding_never_executes_or_rewrites_provider_javascript():
    original = raw('additional-purchase-subset-20261106.js')
    with pytest.raises(ValueError):
        additional.purchase_rows(original+'globalThis.pwned=true', '110011')
    with pytest.raises(ValueError):
        additional.purchase_rows(original.replace('record:"1"', 'record:"2"'), '110011')
    with pytest.raises(ValueError):
        additional.purchase_rows(original, '999999')
    with pytest.raises(ValueError):
        additional.assignment('var apidata={content:call()};', 'apidata')


def test_manager_scope_does_not_turn_into_a_global_or_substring_search(source):
    assert source
    publish(251, observations()[251])
    assert store.query_product(251, {'fundManagerName': '张坤'})['total'] == 1
    with pytest.raises(LookupError):
        store.query_product(251, {'fundManagerName': '张'})
    with pytest.raises(ValueError):
        publish(251, [{'record_key': 'other', 'data': {'fundManagerName': '其他人', 'return1m': 1}}])
    assert store._scopes({'fundCode': '110011', 'fundManagerName': '张坤'}, 252) == ('fund', ['110011'])
    # pylint: disable=protected-access


def test_manager_requires_every_current_fund_in_the_same_observed_interval():
    profile = raw('additional-manager-profile-20261106.html')
    payload = json.loads(raw('additional-manager_curve-20261106.json'))
    data = json.loads(payload['Data'])
    data['graph'].pop()
    payload['Data'] = json.dumps(data)
    with pytest.raises(ValueError):
        additional.manager_rows(profile, payload, '30189744', '张坤')
    with pytest.raises(ValueError):
        additional.manager_rows(profile, payload, '30189744', '同名其他人')


def test_industry_reverse_requires_every_active_catalog_member(source):
    _, _ = source
    rows = observations()[264]
    store.ingest(264, rows, scope_kind='fund', scope_key='110011', window_start='2026-11-06',
                 window_end='2026-11-06', source_name='eastmoney', method_version='em-native-industry-nav-current-v1:real',
                 evidence_uri='https://api.fund.eastmoney.com/f10/HYPZ/', evidence_sha256='a'*64,
                 complete_window=True)
    args = SimpleNamespace(product=262)
    additional._industry_reverse(args)  # pylint: disable=protected-access
    assert store.query_product(262, {'industryCode': '25'})['items'][0]['holdingRatioLatest'] == 27.09
    database.insert('fund_basic_ext', {'fund_code': '005827', 'ts_code': '005827.OF', 'status': 'L', 'source': 'tushare'})
    with pytest.raises(LookupError):
        additional._industry_reverse(args)  # pylint: disable=protected-access


def test_snapshot_fact_deletion_fails_instead_of_becoming_billable_empty_success(source):
    _, db = source
    publish(264, observations()[264])
    with db.transaction() as conn:
        conn.execute('DELETE FROM open_fund_product_fact WHERE product_id=264')
    with pytest.raises(LookupError):
        store.query_product(264, {'fundCode': '110011'})


def test_query_rejects_already_persisted_same_name_different_manager_identities(source):
    assert source
    values = dict(scope_kind='manager', scope_key='张坤', window_start='2026-11-06', window_end='2026-11-06',
                  method_version='current-cohort-v1', evidence_uri='https://fund.eastmoney.com/manager/',
                  evidence_sha256='a'*64, complete_window=True)
    store.ingest(251, observations()[251], source_name='eastmoney:manager:30189744', **values)
    store.ingest(251, observations()[251], source_name='eastmoney:manager:99999999', **values)
    with pytest.raises(LookupError):
        store.query_product(251, {'fundManagerName': '张坤'})


def test_directory_requires_every_page_strict_id_order_and_true_count():
    pages = [raw(f'additional-manager-directory-page-{n}-20261106.js') for n in [1, 2]]
    assert len(additional.directory_rows(pages)) == 3
    with pytest.raises(ValueError):
        additional.directory_rows(pages[:1])
    with pytest.raises(ValueError):
        additional.directory_rows([pages[1], pages[0]])
    with pytest.raises(ValueError):
        additional.directory_rows([pages[0], pages[0]])


def test_first_ingested_same_name_manager_does_not_establish_uniqueness(source):
    from collections import defaultdict  # pylint: disable=import-outside-toplevel
    assert source
    names = defaultdict(list)
    for row in json.loads(raw('additional-complete-manager-directory-20261106.json')):
        names[row['name']].append(row['id'])
    name, identities = next((name, identities) for name, identities in names.items() if len(identities) > 1)
    store.ingest(251, [{'record_key': identities[0], 'data': {'fundManagerName': name, 'return1m': 1.0}}],
                 scope_kind='manager', scope_key=name, window_start='2026-11-06', window_end='2026-11-06',
                 source_name='eastmoney:manager:'+identities[0], method_version='current-cohort-v1',
                 evidence_uri='https://fund.eastmoney.com/manager/', evidence_sha256='a'*64, complete_window=True)
    with pytest.raises(LookupError):
        store.query_product(251, {'fundManagerName': name})


def test_directory_physical_corruption_prevents_success_even_for_unrelated_missing_id(source):
    _, db = source
    publish(251, observations()[251])
    with db.transaction() as conn:
        conn.execute("DELETE FROM open_fund_manager_directory WHERE manager_id='30036320'")
    with pytest.raises(LookupError):
        store.query_product(251, {'fundManagerName': '张坤'})


def test_actual_stock_ytd_ipo_does_not_use_listing_day_as_year_start():
    sample = json.loads(raw('additional-stock-ytd-20261106.json'))
    records = additional.stock_return_rows(sample['portfolio'], sample['quotes'], '161725', '161725.SZ',
                                          '2026-11-06', sample['capitalizations'])
    assert records == sample['expected']
    assert len(records) == 15 and sum('returnYtd' in row['data'] for row in records) == 12
    for row in records:
        if row['data']['stockCode'] in {'001248', '688801', '688825'}:
            assert 'returnYtd' not in row['data'] and row['data']['marketCap'] > 0
    with pytest.raises(ValueError):
        additional.stock_return_rows(sample['portfolio'], sample['quotes'], '161725', '161725.SZ', '2026-11-06')


def test_new_ambiguous_directory_never_resurrects_older_unique_snapshot(source, monkeypatch):
    _, db = source
    with db.transaction() as conn:
        conn.execute('DELETE FROM open_fund_manager_directory')
        conn.execute('DELETE FROM open_fund_manager_directory_batch')
    monkeypatch.setattr(store.time, 'time', lambda: 1000)
    store.publish_manager_directory([{'id': '30000001', 'name': '同名'}], 1, 'c'*64)
    assert store.manager_identity('同名') == '30000001'
    monkeypatch.setattr(store.time, 'time', lambda: 10**10)
    assert store.manager_identity('同名') == '30000001'
    store.publish_manager_directory([{'id': '30000001', 'name': '同名'}, {'id': '30000002', 'name': '同名'}],
                                    2, 'd'*64)
    with pytest.raises(LookupError):
        store.manager_identity('同名')
