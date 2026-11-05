"""Gateway source contracts against a temporary SQLite database."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token

from app import db as database
from app.db.sqlite import SqliteDatabase
from app.open_data.router import bp


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = SqliteDatabase(str(tmp_path / 'test.db'))
    monkeypatch.setitem(database._STATE, 'db', db)  # pylint: disable=protected-access
    database.init_db((Path(__file__).parents[1] / 'schema_sqlite.sql').read_text())
    database.insert('funds', {'code': '110011', 'name': '样例基金', 'type': '混合型', 'fund_type': 'stock'})
    database.insert('fund_details', {'fund_code': '110011', 'return_1m': 5.2, 'scale': 40.3,
                                   'trade_date': '2026-11-04', 'detail_json': 'PRIVATE RAW DETAILS'})
    database.insert('fund_basic_ext', {'fund_code': '110011', 'ts_code': '110011.OF',
                                     'list_date': '2010-01-01', 'management_fee': 1.2})
    app = Flask(__name__)
    app.config['JWT_SECRET_KEY'] = 'x' * 64
    JWTManager(app)
    app.register_blueprint(bp)
    with app.app_context():
        token = create_access_token(identity='gateway-service')
    http = app.test_client()
    http.environ_base['HTTP_AUTHORIZATION'] = f'Bearer {token}'
    yield http


def test_requires_existing_service_jwt(client):
    result = client.get('/api/open-data/v1/fund/list', headers={'Authorization': ''})
    assert result.status_code == 401


def test_nav_has_real_unit_and_accumulated_values_beyond_chart_limit(client):
    start = date(2010, 1, 1)
    database.batch_insert('fund_nav', [{'fund_code': '110011', 'trade_date': (start + timedelta(days=i)).isoformat(),
                                      'nav': 1 + i / 10000, 'acc_nav': 2 + i / 10000,
                                      'adj_nav': 3 + i / 10000, 'adj_src': 'calc'} for i in range(2101)])
    result = client.get('/api/open-data/v1/fund/nav?fund_code=110011&page=5&page_size=500')
    assert result.status_code == 200
    data = result.json
    assert data['total'] == 2101
    assert len(data['items']) == 101
    assert data['items'][0]['nav'] == 1.2
    assert data['items'][0]['acc_nav'] == 2.2
    assert data['items'][0]['adj_nav'] == 3.2
    assert data['items'][0]['adj_src'] == 'calc'
    assert 'fetch_time' not in data['items'][0]
    day = (start + timedelta(days=2000)).isoformat()
    sliced = client.get(f'/api/open-data/v1/fund/nav?fund_code=110011&start_date={day}&end_date={day}')
    assert sliced.json['total'] == 1
    assert sliced.json['items'][0]['trade_date'] == day


@pytest.mark.parametrize('fund_type', ['货币型', '货币型-普通货币', '货币型-浮动净值',
                                      '', None, '未分类', '未来新类型'])
def test_nav_rejects_money_or_unverified_type_without_relabeling_income(client, fund_type):
    database.insert('funds', {'code': '000009', 'name': '待审定基金', 'type': fund_type,
                              'fund_type': 'non_stock'})
    # The legacy money-fund JS collector stores Data_millionCopiesIncome in
    # nav. A calc adjusted value can be derived from that same field, so both
    # raw and adjusted mappings must be barred rather than replacing nav by 1.
    database.insert('fund_nav', {'fund_code': '000009', 'trade_date': '2026-11-04',
                                'nav': .3556, 'adj_nav': .3667, 'adj_src': 'calc'})
    response = client.get('/api/open-data/v1/fund/nav?fund_code=000009')
    assert response.status_code == 503
    assert response.json == {'error': 'product_window_incomplete', 'reason': 'nav_type_unverified'}
    assert 'items' not in response.json


def test_nav_type_check_covers_entire_batch_before_pagination(client):
    database.insert('funds', {'code': '999999', 'name': '货币基金', 'type': '货币型'})
    database.batch_insert('fund_nav', [
        {'fund_code': '110011', 'trade_date': '2026-11-04', 'nav': 1.5},
        {'fund_code': '999999', 'trade_date': '2026-11-04', 'nav': .3556},
    ])
    response = client.get('/api/open-data/v1/fund/nav?fund_codes=110011,999999&page_size=1')
    assert response.status_code == 503
    assert 'total' not in response.json
    # Absence from the master is also an unverified type, even when NAV rows
    # exist; neither an orphan row nor an empty page proves its semantics.
    database.insert('fund_nav', {'fund_code': '000001', 'trade_date': '2026-11-04', 'nav': .4})
    assert client.get('/api/open-data/v1/fund/nav?fund_code=000001&page=2').status_code == 503


@pytest.mark.parametrize('fund_type', ['股票型', '混合型-灵活', '债券型-长债',
                                      '指数型-股票', 'QDII-普通股票', 'FOF-稳健型', 'REITs'])
def test_verified_nav_types_preserve_unit_and_adjusted_values(client, fund_type):
    database.update('funds', {'code': '110011'}, {'type': fund_type, 'fund_type': 'non_stock'})
    database.insert('fund_nav', {'fund_code': '110011', 'trade_date': '2026-11-04',
                                'nav': 1.2, 'acc_nav': 2.3, 'adj_nav': 3.4, 'adj_src': 'tushare'})
    response = client.get('/api/open-data/v1/fund/nav?fund_code=110011')
    assert response.status_code == 200
    assert response.json['items'][0]['nav'] == 1.2
    assert response.json['items'][0]['acc_nav'] == 2.3
    assert response.json['items'][0]['adj_nav'] == 3.4


def test_master_joins_extension_and_excludes_private_payloads(client):
    data = client.get('/api/open-data/v1/fund/basic-info?fund_code=110011').json
    assert data['items'][0]['list_date'] == '2010-01-01'
    assert data['items'][0]['scale'] == 40.3
    assert data['units']['scale'] == '100m_CNY'
    assert 'detail_json' not in data['items'][0]
    assert 'holdings' not in data['items'][0]
    assert 'ai' not in data['items'][0]
    assert client.get('/api/open-data/v1/fund/list?page=2').json['total'] == 1
    assert client.get('/api/open-data/v1/fund/list?page=2').json['items'] == []


def test_return_snapshot_has_percentage_units(client):
    data = client.get('/api/open-data/v1/fund/returns?fund_code=110011').json
    assert data['items'][0]['return_1m'] == 5.2
    assert data['units']['returns'] == 'percent'
    assert data['items'][0]['trade_date'] == '2026-11-04'


def test_exact_holdings_never_substitutes_missing_quarter(client):
    database.batch_insert('fund_holdings', [
        {'fund_code': '110011', 'quarter': '2026Q3', 'holding_type': 'stock',
         'asset_code': '600570', 'hold_ratio': 5, 'raw_data': 'private'},
        {'fund_code': '110011', 'quarter': '2026Q3', 'holding_type': 'stock',
         'asset_code': '000001', 'hold_ratio': 5},
    ])
    missing = client.get('/api/open-data/v1/fund/holdings?fund_code=110011&quarter=2025Q4')
    assert missing.json['total'] == 0
    assert missing.json['quarter'] == '2025Q4'
    latest = client.get('/api/open-data/v1/fund/holdings?fund_code=110011&page_size=1')
    assert latest.json['quarter'] == '2026Q3'
    assert latest.json['total'] == 2
    assert latest.json['items'][0]['asset_code'] == '000001'
    assert 'raw_data' not in latest.json['items'][0]


def test_events_preserve_actual_event_types_and_dates(client):
    database.batch_insert('fund_div_split', [
        {'fund_code': '110011', 'ex_date': '2026-01-01', 'event_type': 'div', 'cash_per_unit': .1, 'split_ratio': None},
        {'fund_code': '110011', 'ex_date': '2026-02-01', 'event_type': 'split', 'cash_per_unit': None, 'split_ratio': 2},
    ])
    data = client.get('/api/open-data/v1/fund/events?fund_code=110011&event_type=split').json
    assert data['total'] == 1
    assert data['items'][0]['event_type'] == 'split'
    assert data['items'][0]['split_ratio'] == 2
    assert client.get('/api/open-data/v1/fund/events?fund_code=110011&event_type=unknown').status_code == 400


def test_manager_current_filter_happens_before_count_and_paging(client):
    database.batch_insert('fund_manager_tenure', [
        {'fund_code': '110011', 'seq': 0, 'managers': '甲 乙', 'is_current': 1,
         'tenure_return': 25.5, 'tenure_days': 400},
        {'fund_code': '110011', 'seq': 1, 'managers': '丙', 'is_current': 0, 'tenure_return': 10},
    ])
    data = client.get('/api/open-data/v1/fund/manager-tenure?fund_code=110011&is_current=1').json
    assert data['total'] == 1
    assert data['items'][0]['managers'] == '甲 乙'
    assert data['items'][0]['tenure_return'] == 25.5
    assert 'returnAnn' not in data['items'][0]


def test_share_snapshots_retain_units(client):
    database.insert('fund_share', {'fund_code': '110011', 'ts_code': '110011.OF',
                                  'trade_date': '2026-11-04', 'share_value': 1234})
    row = client.get('/api/open-data/v1/fund/shares?fund_code=110011').json['items'][0]
    assert row['share_value'] == 1234
    assert row['share_unit'] == '10k_shares'
    assert 'subscribeShare' not in row


def test_categories_and_fee_supported_subset(client):
    database.insert('fund_types', {'type_name': '混合型', 'category': 'stock'})
    assert client.get('/api/open-data/v1/fund/categories').json['items'][0]['type_name'] == '混合型'
    row = client.get('/api/open-data/v1/fund/fees?fund_code=110011').json['items'][0]
    assert row['management_fee'] == 1.2
    assert row['sales_service_fee'] is None


@pytest.mark.parametrize('query', [
    'fund_code=110011&fund_code=000001', 'fund_code=110011&table=users',
    'fund_code=110011&page_size=501', 'fund_code=110011&page=0',
    'fund_code=110011&start_date=20261104', 'fund_code=110011&start_date=2026-02-30',
    'fund_code=110011&start_date=2026-11-05&end_date=2026-11-04',
    'fund_code=110011%27%20OR%201=1',
])
def test_query_validation(client, query):
    assert client.get('/api/open-data/v1/fund/nav?' + query).status_code == 400


def test_batch_funds_count_and_filter_before_global_paging(client):
    database.insert('funds', {'code': '000001', 'name': '第二基金', 'type': '债券型'})
    database.batch_insert('fund_nav', [
        {'fund_code': '110011', 'trade_date': '2026-11-04', 'nav': 1, 'acc_nav': 2},
        {'fund_code': '000001', 'trade_date': '2026-11-04', 'nav': 3, 'acc_nav': 4},
    ])
    data = client.get('/api/open-data/v1/fund/nav?fund_codes=110011,000001&page_size=1').json
    assert data['total'] == 2
    assert data['items'][0]['fund_code'] == '000001'
    second = client.get('/api/open-data/v1/fund/nav?fund_codes=110011,000001&page_size=1&page=2').json
    assert second['total'] == 2
    assert second['items'][0]['fund_code'] == '110011'
    assert client.get('/api/open-data/v1/fund/nav?fund_code=110011&fund_codes=000001').status_code == 400
    assert client.get('/api/open-data/v1/fund/list?fund_codes=110011,000001&page_size=1').json['total'] == 2


def test_read_snapshot_does_not_take_sqlite_writer_lock(client):
    import sqlite3

    db = database.get_db()
    with db.transaction(read_only=True):
        assert database.count('funds') == 1
        with sqlite3.connect(db._db_path, timeout=.1) as writer:  # pylint: disable=protected-access
            writer.execute("INSERT INTO funds(code,name) VALUES('000001','Concurrent writer')")
        # Both COUNT and SELECT retain the same snapshot despite a committed sync.
        assert len(database.select('funds')) == 1
    assert database.count('funds') == 2


def test_mysql_read_snapshot_uses_read_only_transaction():
    from unittest.mock import MagicMock, patch
    from app.db.mysql import MysqlDatabase

    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = []
    cursor.fetchone.return_value = {'n': 0}
    with patch('app.db.mysql.pymysql.connect', return_value=connection):
        db = MysqlDatabase()
        try:
            with db.transaction(read_only=True):
                database_rows = db.select('fund_nav', {'fund_code': 'eq.110011'})
                count = db.count('fund_nav', {'fund_code': 'eq.110011'})
            assert database_rows == []
            assert count == 0
            assert cursor.execute.call_args_list[0].args == ('START TRANSACTION READ ONLY',)
            connection.begin.assert_not_called()
            connection.commit.assert_called_once()
        finally:
            db.close()


def test_reference_fee_rows_paginate_after_expansion_and_preserve_zero(client):
    database.update('fund_basic_ext', {'fund_code': '110011'}, {'custodian_fee': 0})
    first = client.get('/api/open-data/v1/fund/reference-fees?fund_code=110011&page_size=1').json
    second = client.get('/api/open-data/v1/fund/reference-fees?fund_code=110011&page_size=1&page=2').json
    assert first['total'] == second['total'] == 2
    assert first['items'][0]['fee_kind'] == '管理费'
    assert first['items'][0]['fee_percent'] == 1.2
    assert second['items'][0]['fee_kind'] == '托管费'
    assert second['items'][0]['fee_percent'] == 0
    assert first['units']['fee_percent'] == 'percent'
    assert first['items'][0]['fund_name'] == '样例基金'
    assert client.get('/api/open-data/v1/fund/reference-fees?fund_code=110011&start_date=2026-01-01').status_code == 400


def test_reference_share_snapshots_use_unique_canonical_role_and_real_count(client):
    database.batch_insert('fund_share', [
        {'fund_code': '110011', 'ts_code': '110011.OF', 'trade_date': '2026-11-03',
         'share_value': 10, 'share_unit': '10k_shares'},
        {'fund_code': '110011', 'ts_code': '110011.OF', 'trade_date': '2026-11-04',
         'share_value': 20, 'share_unit': '10k_shares'},
        {'fund_code': '110011', 'ts_code': '110011.SH', 'trade_date': '2026-11-04',
         'share_value': 999, 'share_unit': '10k_shares'},
    ])
    data = client.get('/api/open-data/v1/fund/reference-shares?fund_code=110011&page_size=1&page=2').json
    assert data['total'] == 2
    assert data['items'][0]['share_value'] == 20
    assert data['items'][0]['ts_code'] == '110011.OF'
    assert data['items'][0]['source_field'] == 'fd_share'
    assert data['items'][0]['share_unit'] == '10k_shares'
    sliced = client.get('/api/open-data/v1/fund/reference-shares?fund_code=110011&start_date=2026-11-04').json
    assert sliced['total'] == 1
    assert 'subscribeShare' not in sliced['items'][0]


def test_reference_share_rejects_unknown_role_and_unit_before_pagination(client):
    assert client.get('/api/open-data/v1/fund/reference-shares?fund_code=000001').status_code == 503
    database.batch_insert('fund_share', [
        {'fund_code': '110011', 'ts_code': '110011.OF', 'trade_date': '2026-11-03',
         'share_value': 10, 'share_unit': '10k_shares'},
        {'fund_code': '110011', 'ts_code': '110011.OF', 'trade_date': '2026-11-04',
         'share_value': 20, 'share_unit': 'shares'},
    ])
    response = client.get('/api/open-data/v1/fund/reference-shares?fund_code=110011&page_size=1')
    assert response.status_code == 503
    assert response.json['reason'] == 'share_unit_unverified'


def test_reference_listings_and_risk_use_actual_source_values(client):
    database.update('fund_basic_ext', {'fund_code': '110011'},
                    {'issue_date': '2010-01-01', 'purchase_start_date': '2010-03-01',
                     'redemption_start_date': '2010-03-02', 'par_value': 1})
    row = client.get('/api/open-data/v1/fund/reference-listings?fund_code=110011').json['items'][0]
    assert row['issue_date'] == '2010-01-01'
    assert row['par_value'] == 1
    assert 'issue_amount' not in row
    database.update('fund_details', {'fund_code': '110011'},
                    {'volatility_1y': 23.5, 'sharpe_1y': 1.2, 'max_drawdown_1y': 8.5})
    row = client.get('/api/open-data/v1/fund/reference-performance?fund_code=110011').json['items'][0]
    assert row['volatility_1y'] == 23.5
    assert row['sharpe_1y'] == 1.2
    assert row['max_drawdown_1y'] == 8.5
    assert 'trade_date' not in row
    assert 'detail_json' not in row
    assert client.get('/api/open-data/v1/fund/reference-performance?fund_code=110011&end_date=2026-11-01').status_code == 400


def test_reference_categories_keep_actual_type_and_manager_filters_apply_before_paging(client):
    categories = client.get('/api/open-data/v1/fund/reference-categories?fund_codes=110011').json
    assert categories['items'] == [{'fund_code': '110011', 'fund_name': '样例基金', 'type': '混合型'}]
    database.batch_insert('fund_manager_tenure', [
        {'fund_code': '110011', 'seq': 0, 'managers': '甲 乙', 'is_current': 1},
        {'fund_code': '110011', 'seq': 1, 'managers': '甲', 'is_current': 0},
        {'fund_code': '110011', 'seq': 2, 'managers': '甲丙', 'is_current': 0},
    ])
    data = client.get('/api/open-data/v1/fund/manager-tenure?fund_code=110011&manager_name=甲&is_current=0').json
    assert data['total'] == 1
    assert data['items'][0]['managers'] == '甲'
    assert data['items'][0]['fund_name'] == '样例基金'


def test_empty_tables_remain_empty_but_existing_placeholder_snapshots_fail_quality(client):
    assert client.get('/api/open-data/v1/fund/reference-performance?fund_code=000001').json['items'] == []
    assert client.get('/api/open-data/v1/fund/reference-listings?fund_code=000001').json['items'] == []
    assert client.get('/api/open-data/v1/fund/reference-fees?fund_code=000001').json['items'] == []
    database.insert('fund_details', {'fund_code': '000001'})
    database.insert('fund_basic_ext', {'fund_code': '000001', 'ts_code': '000001.OF'})
    for path in ('reference-performance', 'reference-listings', 'reference-fees'):
        response = client.get('/api/open-data/v1/fund/' + path + '?fund_code=000001')
        assert response.status_code == 503
        assert response.json['error'] == 'product_window_incomplete'
