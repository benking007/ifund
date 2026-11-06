"""Cold-start source producers, atomic evidence and independent NAV semantics."""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from app import db as database
from app.db.sqlite import SqliteDatabase
from app.open_data import product_base as base, product_store as store


@pytest.fixture
def db(tmp_path, monkeypatch):
    connection = SqliteDatabase(str(tmp_path/'base.db'))
    monkeypatch.setitem(database._STATE, 'db', connection)  # pylint: disable=protected-access
    database.init_db((Path(__file__).parents[1]/'schema_sqlite.sql').read_text())
    store.migrate()
    return connection


def args(kind, **values):
    return Namespace(kind=kind, fund_code=None, start='0001-01-01', end='2026-11-05', ttl_seconds=86400,
                     lookback_days=7, **values)


def watermark_count():
    with database.get_db().transaction(read_only=True) as conn:
        return store._sql(conn, 'SELECT COUNT(*) AS n FROM open_fund_base_watermark', fetch=True)[0]['n']


def seed():
    database.insert('funds', {'code': '110011', 'name': 'actual', 'type': '混合型'})
    database.insert('fund_basic_ext', {'fund_code': '110011', 'ts_code': '110011.OF', 'status': 'L',
                                     'source': 'tushare', 'market': 'O'})


def test_empty_database_bootstraps_actual_master_and_company_with_exact_roles(db, monkeypatch):
    assert db
    def provider(operation, params, _fields=''):
        if operation == 'fund_company':
            return [{'name': '甲基金管理有限公司', 'shortname': '甲基金'}]
        return [{'ts_code': '510300.SH' if params['market'] == 'E' else '110011.OF',
                 'name': 'actual', 'market': params['market'], 'status': 'L', 'fund_type': '混合型',
                 'management': '甲基金', 'found_date': '20000101'}]
    monkeypatch.setattr(base.collectors, 'collect_tushare', provider)
    result = base.collect(args('base-master'))
    assert result['quality']['row_count'] == 2
    assert database.select_one('fund_basic_ext', {'fund_code': 'eq.110011'})['company_id'] == 1
    assert database.select_one('funds', {'code': 'eq.510300'})['type'] == '混合型'
    assert watermark_count() == 1


def test_invalid_second_market_is_atomic_and_never_publishes_empty_master(db, monkeypatch):
    assert db
    monkeypatch.setattr(base.collectors, 'collect_tushare', lambda *_: [])
    with pytest.raises(ValueError, match='complete market'):
        base.collect(args('base-master'))
    assert database.count('funds') == watermark_count() == 0


def test_nav_keeps_independent_values_and_excludes_money_income(db, monkeypatch):
    assert db
    seed()
    database.insert('funds', {'code': '000009', 'name': 'money', 'type': '货币市场型'})
    database.insert('fund_basic_ext', {'fund_code': '000009', 'ts_code': '000009.OF', 'source': 'tushare'})
    raw = [{'ts_code': '110011.OF', 'nav_date': '20261105', 'unit_nav': 4, 'accum_nav': 6, 'adj_nav': 8},
           {'ts_code': '000009.OF', 'nav_date': '20261105', 'unit_nav': .1}]
    monkeypatch.setattr(base.collectors, 'collect_tushare', lambda *_: raw)
    value = args('base-nav')
    value.start = value.end
    result = base.collect(value)
    assert result['quality']['row_count'] == 1 and result['quality']['unverified_or_money_rows_excluded'] == 1
    row = database.select_one('fund_nav', {'fund_code': 'eq.110011'})
    assert (row['nav'], row['acc_nav'], row['adj_nav'], row['adj_src']) == (4, 6, 8, 'tushare')
    assert database.count('fund_nav') == 1


def test_wrong_date_source_fails_before_any_nav_mutation(db, monkeypatch):
    assert db
    seed()
    monkeypatch.setattr(base.collectors, 'collect_tushare', lambda *_: [
        {'ts_code': '110011.OF', 'nav_date': '20260101', 'unit_nav': 4}])
    value = args('base-nav')
    value.start = value.end
    with pytest.raises(ValueError, match='exact date'):
        base.collect(value)
    assert database.count('fund_nav') == watermark_count() == 0


def test_dividends_preserve_per_unit_tax_unspecified_semantics(db, monkeypatch):
    assert db
    seed()
    monkeypatch.setattr(base.collectors, 'collect_tushare', lambda *_: [
        {'ts_code': '110011.OF', 'ex_date': '20260101', 'div_cash': .3}])
    value = args('base-dividends')
    value.fund_code = '110011'
    assert base.collect(value)['quality']['row_count'] == 1
    event = database.select_one('fund_div_split')
    assert event['cash_per_unit'] == .3 and event['event_type'] == 'div'
    assert event['split_ratio'] is None


def test_return_intervals_use_calendar_anchor_not_fixed_row_count(db):
    assert db
    seed()
    database.batch_insert('fund_nav', [
        {'fund_code': '110011', 'trade_date': '2026-10-02', 'adj_nav': 10, 'adj_src': 'tushare'},
        {'fund_code': '110011', 'trade_date': '2026-11-05', 'adj_nav': 11, 'adj_src': 'tushare'}])
    value = args('base-returns')
    value.fund_code = '110011'
    assert base.collect(value)['quality']['row_count'] == 1
    row = database.select_one('fund_details', {'fund_code': 'eq.110011'})
    assert row['return_1m'] == pytest.approx(10)
    assert row['return_1y'] is None and row['return_since_inception'] is None


def test_actual_split_ratio_direction_and_literal_zero():
    fixtures = Path(__file__).parent/'fixtures'
    rows = base.split_rows((fixtures/'splits-510300-20261106.html').read_text(), '510300')
    assert len(rows) == 1 and rows[0]['ex_date'] == '2012-05-11' and rows[0]['split_ratio'] == .3709
    assert base.split_rows((fixtures/'splits-110011-20261106.html').read_text(), '110011') == []
    with pytest.raises(ValueError, match='identity'):
        base.split_rows((fixtures/'splits-510300-20261106.html').read_text(), '110011')


def test_legacy_seven_digit_provider_role_is_quarantined_not_truncated(db, monkeypatch):
    assert db
    def provider(operation, params, _fields=''):
        if operation == 'fund_company':
            return [{'name': 'actual-company'}]
        if params['market'] == 'E':
            return [{'ts_code': '510300.SH', 'name': 'actual', 'market': 'E', 'status': 'L'},
                    {'ts_code': '1610221.SZ', 'name': 'old-role', 'market': 'E', 'status': 'D'}]
        return [{'ts_code': '110011.OF', 'name': 'actual', 'market': 'O', 'status': 'L'}]
    monkeypatch.setattr(base.collectors, 'collect_tushare', provider)
    result = base.collect(args('base-master'))
    assert result['quality']['unsupported_code_roles_excluded'] == 1
    assert result['quality']['row_count'] == 2
    assert database.select_one('funds', {'code': 'eq.161022'}) is None


def test_master_update_preserves_exact_original_granular_classification(db, monkeypatch):
    assert db
    seed()
    database.update('funds', {'code': '110011'}, {'type': 'FOF-稳健型'})
    def provider(operation, params, _fields=''):
        if operation == 'fund_company':
            return [{'name': 'exact-company'}]
        return [{'ts_code': '510300.SH' if params['market']=='E' else '110011.OF',
                 'name': 'actual', 'market': params['market'], 'status': 'L', 'fund_type': '混合型'}]
    monkeypatch.setattr(base.collectors, 'collect_tushare', provider)
    base.collect(args('base-master'))
    assert database.select_one('funds', {'code': 'eq.110011'})['type'] == 'FOF-稳健型'


def test_new_classification_unknown_does_not_resurrect_old_fof_label(db, monkeypatch):
    from types import SimpleNamespace  # pylint: disable=import-outside-toplevel
    import sys  # pylint: disable=import-outside-toplevel
    import json  # pylint: disable=import-outside-toplevel
    assert db
    seed()
    database.update('fund_basic_ext', {'fund_code': '110011'}, {'fund_type': '混合型'})
    database.insert('funds', {'code': '510300', 'name': 'actual', 'type': '股票型'})
    database.insert('fund_basic_ext', {'fund_code': '510300', 'ts_code': '510300.SH', 'fund_type': '股票型'})
    rows = [{'基金代码': '110011', '基金简称': 'actual', '基金类型': 'FOF-稳健型'},
            {'基金代码': '510300', '基金简称': 'actual', '基金类型': '指数型-股票'}]
    class Frame:
        columns = {'基金代码', '基金简称', '基金类型'}
        def __len__(self):
            return len(rows)
        def to_json(self, **_kwargs):
            return json.dumps(rows)
    monkeypatch.setitem(sys.modules, 'akshare', SimpleNamespace(fund_name_em=Frame))
    base.collect(args('base-categories'))
    assert database.select_one('funds', {'code': 'eq.110011'})['type'] == 'FOF-稳健型'
    rows[0]['基金类型'] = None
    result = base.collect(args('base-categories'))
    assert result['quality']['canonical_missing_or_unknown_category'] == 1
    assert database.select_one('funds', {'code': 'eq.110011'})['type'] == '混合型'
