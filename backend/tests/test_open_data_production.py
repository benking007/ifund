"""Dedicated production entrypoint/migration/readiness and batch read checks."""
from __future__ import annotations

from pathlib import Path
import sys

import pytest
from flask_jwt_extended import create_access_token

from app import db as database
from app.db.sqlite import SqliteDatabase
from app.open_data import product_store as store
from app.open_data.product_worker import main

sys.path.insert(0, str(Path(__file__).parents[1]))
from open_data_service import create_app  # pylint: disable=wrong-import-position


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv('SECRET_KEY', 'production-test-strong-secret-'+'x'*32)
    db = SqliteDatabase(str(tmp_path/'isolated.db'))
    monkeypatch.setitem(database._STATE, 'db', db)  # pylint: disable=protected-access
    assert main(['migrate', '--base-schema']) == 0
    application = create_app()
    with application.app_context():
        token = create_access_token(identity='source-fixture')
    client = application.test_client()
    client.environ_base['HTTP_AUTHORIZATION'] = 'Bearer '+token
    return client, application


def test_production_rejects_weak_secret_without_mutating_db(monkeypatch):
    monkeypatch.delenv('SECRET_KEY', raising=False)
    with pytest.raises(RuntimeError, match='strong'):
        create_app()


def test_migration_binlog_trigger_failure_has_fixed_secret_free_diagnostic(monkeypatch, capsys):
    def denied():
        raise RuntimeError(1419, 'private-db-host password=must-not-appear')
    monkeypatch.setattr(store, 'migrate', denied)
    assert main(['migrate']) == 1
    output = capsys.readouterr().out
    assert 'MYSQL_TRIGGER_BINLOG_DBA_CONFIGURATION_REQUIRED' in output
    assert 'log_bin_trust_function_creators=1' in output
    assert 'must-not-appear' not in output and 'private-db-host' not in output


def test_dedicated_service_has_no_registration_or_personal_api(app):
    client, _ = app
    assert client.post('/api/auth/register', json={'username': 'intruder', 'password': 'xxxxxxxx'}).status_code == 404
    assert client.get('/api/portfolios').status_code == 404
    assert client.post('/api/auth/token/exchange', headers={'Authorization': ''}).status_code == 401
    assert client.get('/api/open-data/v1/_ready', headers={'Authorization': ''}).status_code == 401


def test_schema_only_empty_database_never_claims_data_ready(app):
    client, _ = app
    response = client.get('/api/open-data/v1/_ready')
    assert response.status_code == 503
    assert response.json['database'] == 'ok'
    assert response.json['schemaVersion'] == 6
    assert response.json['fresh_product_count'] == 0
    store.ingest(238, [{'record_key': 'status', 'data': {'fundCode': '110011', 'subscribeLimit': 50000}}],
                 scope_kind='fund', scope_key='110011', window_start='2026-11-06', window_end='2026-11-06',
                 source_name='actual-snapshot', method_version='v1', evidence_uri='https://fundf10.eastmoney.com/',
                 evidence_sha256='a'*64, complete_window=True)
    assert client.get('/api/open-data/v1/_ready').status_code == 200


def test_batch_holdings_keep_each_actual_quarter_and_global_page(app):
    client, _ = app
    database.batch_insert('fund_holdings', [
        {'fund_code': '110011', 'quarter': '2026Q3', 'holding_type': 'stock', 'asset_code': '600519', 'hold_ratio': 4},
        {'fund_code': '110011', 'quarter': '2026Q2', 'holding_type': 'stock', 'asset_code': '600570', 'hold_ratio': 20},
        {'fund_code': '006581', 'quarter': '2026Q2', 'holding_type': 'stock', 'asset_code': '000001', 'hold_ratio': 3},
    ])
    response = client.get('/api/open-data/v1/fund/holdings?fund_codes=110011,006581&page_size=1&page=2')
    assert response.status_code == 200
    assert response.json['total'] == 2
    assert response.json['items'][0]['asset_code'] == '000001'
    assert {row['fund_code']: row['quarter'] for row in response.json['quarters']} == {
        '110011': '2026Q3', '006581': '2026Q2'}
    assert client.get('/api/open-data/v1/fund/holdings?fund_codes=110011,999999').status_code == 503


def test_tushare_secret_accepts_plain_mounted_token_and_legacy_dotenv(tmp_path):
    from app.fund_nav.fetch.tushare_client import _token_from_file  # pylint: disable=import-outside-toplevel,protected-access
    secret = tmp_path/'secret'
    secret.write_text('a'*56+'\n')
    assert _token_from_file(secret) == 'a'*56
    secret.write_text('TUSHARE_TOKEN="'+'b'*56+'"\n')
    assert _token_from_file(secret) == 'b'*56
    secret.write_text('not-a-token\n')
    assert _token_from_file(secret) == ''


def test_non_disclosure_is_not_a_verified_empty_or_runtime_success(monkeypatch, capsys):
    from app.open_data import product_worker as worker  # pylint: disable=import-outside-toplevel
    from app.open_data.product_outcomes import NotDisclosedError  # pylint: disable=import-outside-toplevel
    def unavailable(_args):
        raise NotDisclosedError('no_report_disclosed', 'official-complete-index', {'index_sha256': 'a'*64})
    monkeypatch.setattr(worker, 'collect', unavailable)
    assert worker.main(['collect', '--kind', 'quarter-report', '--fund-code', '999999']) == 0
    import json  # pylint: disable=import-outside-toplevel
    result = json.loads(capsys.readouterr().out)
    assert result['quality']['status'] == 'not_disclosed'
    assert result['quality']['verified_empty'] is False and result['quality']['complete'] is True and result['quality']['evidence']


def test_scope_generators_declare_configured_and_positive_stock_subsets(app, tmp_path, monkeypatch):
    import json  # pylint: disable=import-outside-toplevel
    from app.open_data.product_universe import members  # pylint: disable=import-outside-toplevel
    assert app
    database.batch_insert('funds', [{'code': '510300', 'name': 'actualETF', 'type': '指数型-股票'},
                                  {'code': '110011', 'name': 'actualQDII', 'type': 'QDII-混合偏股'},
                                  {'code': '000001', 'name': 'actualBond', 'type': '债券型-纯债'}])
    database.batch_insert('fund_basic_ext', [{'fund_code': '510300', 'ts_code': '510300.SH', 'source': 'tushare',
                                             'status': 'L', 'market': 'E', 'benchmark': '沪深300指数'},
                                            {'fund_code': '110011', 'ts_code': '110011.OF', 'source': 'tushare',
                                             'status': 'L', 'market': 'O'},
                                            {'fund_code': '000001', 'ts_code': '000001.OF', 'source': 'tushare',
                                             'status': 'L', 'market': 'O'}])
    database.insert('fund_details', {'fund_code': '110011', 'position_stock': 87.0, 'trade_date': '2026-11-05'})
    assert members('stock_holding_funds')['members'] == []  # Legacy detail date cannot renew allocation.
    store.ingest(259, [{'record_key': 'actual-report', 'data': {'fundCode': '110011', 'stockRatio': 87.0}}],
                 scope_kind='fund', scope_key='110011', window_start='2026-09-30', window_end='2026-09-30',
                 source_name='issuer:actual-allocation', method_version='exact-report-v1',
                 evidence_uri='https://fundf10.eastmoney.com/', evidence_sha256='a'*64, complete_window=True)
    stocks = members('stock_holding_funds')
    assert [row['fund_code'] for row in stocks['members']] == ['110011']
    assert stocks['excluded_or_unknown_stock_allocation'] == 2 and stocks['coverage'] == 'verified_eligible_subset'
    roles = tmp_path/'approved.json'
    roles.write_text(json.dumps({'510300': {'verified': True, 'code': '000300', 'ts_code': '000300.SH',
                                          'fund_benchmark_expression': '沪深300指数',
                                          'evidence_uri': 'https://fundf10.eastmoney.com/jbgk_510300.html'}}))
    monkeypatch.setenv('IFUND_OPEN_BENCHMARK_ROLES_FILE', str(roles))
    benchmarks = members('benchmark_verified_funds')
    assert benchmarks['coverage'] == 'configured_subset_not_all_funds'
    assert benchmarks['unconfigured_funds'] == 2 and len(benchmarks['members']) == 1


def test_prepared_global_dependency_does_not_claim_published_product_ready(app):
    client, _ = app
    store.ingest(252, [{'record_key': 'team', 'data': {'fundCode': '110011', 'fundManagerName': '真实经理',
                                                   'tenureStartDate': '2026-01-01', 'isIncumbent': 1}}],
                 scope_kind='fund', scope_key='110011', window_start='2026-11-06', window_end='2026-11-06',
                 source_name='real-history-preparation', method_version='v1',
                 evidence_uri='https://fundf10.eastmoney.com/', evidence_sha256='a'*64, complete_window=True)
    result = client.get('/api/open-data/v1/_ready')
    assert result.status_code == 503 and result.json['fresh_product_count'] == 0
    assert result.json['products'][0]['stage'] == 'preparation'


def test_operator_pat_is_owned_hash_only_private_file_and_revoked(app, tmp_path, capsys):
    import hashlib  # pylint: disable=import-outside-toplevel
    import stat  # pylint: disable=import-outside-toplevel
    import json  # pylint: disable=import-outside-toplevel
    from app.open_data import service_pat  # pylint: disable=import-outside-toplevel
    client, _ = app
    secret = tmp_path/'reader-pat'
    assert service_pat.main(['create', '--create-service-user', 'svc-open-data', '--name', 'gateway-reader',
                             '--output', str(secret)]) == 0
    diagnostic = capsys.readouterr().out
    assert 'ifd_' not in diagnostic and 'token_hash' not in diagnostic
    result = json.loads(diagnostic)
    raw = secret.read_text().strip()
    assert raw.startswith('ifd_') and stat.S_IMODE(secret.stat().st_mode) == 0o600
    row = database.select_one('api_tokens', {'id': f"eq.{result['token_id']}"})
    assert row['token_hash'] == hashlib.sha256(raw.encode()).hexdigest() and row['token_hash'] != raw
    assert client.post('/api/auth/token/exchange', headers={'Authorization': 'Bearer '+raw}).status_code == 200
    with pytest.raises(ValueError):
        service_pat.revoke(user_id=result['user_id']+1, token_id=result['token_id'])
    assert service_pat.main(['revoke', '--user-id', str(result['user_id']),
                             '--token-id', str(result['token_id'])]) == 0
    assert client.post('/api/auth/token/exchange', headers={'Authorization': 'Bearer '+raw}).status_code == 401
    assert service_pat.main(['create', '--user-id', str(result['user_id']), '--name', 'refuse-overwrite',
                             '--output', str(secret)]) == 1
    assert secret.read_text().strip() == raw and 'ifd_' not in capsys.readouterr().out


def test_strict_reader_serves_any_age_and_bad_new_fund_scope_cannot_revive_old_global(app, monkeypatch):
    import time  # pylint: disable=import-outside-toplevel
    from argparse import Namespace  # pylint: disable=import-outside-toplevel
    from app.open_data import product_base_read  # pylint: disable=import-outside-toplevel
    from app.open_data.product_base import _publish  # pylint: disable=import-outside-toplevel,protected-access
    client, _ = app
    monkeypatch.setenv('IFUND_OPEN_REQUIRE_BASE_WATERMARK', '1')
    database.insert('funds', {'code': '110011', 'name': 'actual', 'type': '混合型'})
    database.insert('fund_nav', {'fund_code': '110011', 'trade_date': '2026-11-05', 'nav': 4,
                                 'acc_nav': 6, 'adj_nav': 8, 'adj_src': 'tushare'})
    assert client.get('/api/open-data/v1/fund/nav?fund_code=110011').status_code == 503
    args = Namespace(kind='base-nav', fund_code=None)
    with database.get_db().transaction() as conn:
        _publish(conn, args, 1, {'isolated': 'verified-source'}, '2026-11-05', '2026-11-05')
    assert client.get('/api/open-data/v1/fund/nav?fund_code=110011').status_code == 200
    real_time = time.time
    monkeypatch.setattr(product_base_read.time, 'time', lambda: real_time()+365*86400)
    assert client.get('/api/open-data/v1/fund/nav?fund_code=110011').status_code == 200
    monkeypatch.setattr(product_base_read.time, 'time', real_time)
    args.fund_code = '110011'
    with database.get_db().transaction() as conn:
        _publish(conn, args, 1, {'isolated': 'new-source'}, '2026-11-05', '2026-11-05')
        store._sql(conn, "UPDATE open_fund_base_watermark SET observed_at=? WHERE scope_id='110011'",
                   (time.time()+3600,))
    assert client.get('/api/open-data/v1/fund/nav?fund_code=110011').status_code == 503


def test_default_tushare_transport_is_official_tls(monkeypatch):
    import os  # pylint: disable=import-outside-toplevel
    import subprocess  # pylint: disable=import-outside-toplevel
    monkeypatch.delenv('TUSHARE_ENDPOINT', raising=False)
    result = subprocess.run([sys.executable, '-c',
                             'from app.fund_nav.fetch.tushare_client import ENDPOINT; print(ENDPOINT)'],
                            capture_output=True, text=True, env=dict(os.environ), check=True)
    assert result.stdout.strip() == 'https://api.tushare.pro'
