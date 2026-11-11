"""Whole-cohort publication, immutable shared selectors and real mapping rows."""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path
import hashlib
import json
import time

import pytest

from app import db as database
from app.db.sqlite import SqliteDatabase
from app.open_data import product_etf_generation as gen, product_store as store
from app.open_data.product_etf_schema import MAPPING_METHODS

REAL = json.loads((Path(__file__).parent/'fixtures/etf-510300-full-annual-midyear-mappings-20261106.json').read_text())


@pytest.fixture
def db(tmp_path, monkeypatch):
    db = SqliteDatabase(str(tmp_path/'versions.db'))
    monkeypatch.setitem(database._STATE, 'db', db)  # pylint: disable=protected-access
    database.init_db((Path(__file__).parents[1]/'schema_sqlite.sql').read_text())
    store.migrate()
    return db


def args(kind):
    return Namespace(kind=kind)


def directory(monkeypatch, codes=('510300', '158008'), observed_at=None):
    # A controlled two-member test cohort, not a claim about the live national denominator.
    rows = sorted([{'fund_code': code, 'symbol': code+('.SH' if code=='510300' else '.SZ'),
                    'name': 'actual-source-role', 'found_date': '2026-08-19' if code=='158008' else '2012-05-04'}
                   for code in codes], key=lambda row: row['symbol'])
    body = {'source': 'fin_data_etf_directory', 'product': 'listed_etfs', 'complete': True,
            'items': rows, 'listed_count': len(rows), 'source_snapshot': 'a'*32,
            'directory_version': hashlib.sha256(gen._encode(rows).encode()).hexdigest(),
            'observed_at': time.time() if observed_at is None else observed_at}
    monkeypatch.setattr(gen.concepts, '_source', lambda *_args, **_kwargs: body)
    gen.sync_directory(args('etf-directory'))
    return body


def stage(product, cohort, *, rows=None):
    rows = REAL[str(product)] if rows is None else rows
    return store.ingest(product, [{'record_key': str(index), 'data': row, 'is_etf': True}
                                 for index, row in enumerate(rows)],
                        scope_kind='fund', scope_key='510300', window_start='2025-12-31', window_end='2026-06-30',
                        source_name='issuer:complete-etf-portfolio+governed-classification',
                        method_version=MAPPING_METHODS[product], evidence_uri='https://pdf.dfcfw.com/',
                        evidence_sha256='b'*64, complete_window=True,
                        scope_metadata={'directory_version': cohort['directory_version'],
                                        'scope_version': gen.scope_version(next(row for row in cohort['items']
                                                                               if row['fund_code']=='510300'))})


def absence(product, cohort, code='158008'):
    row = next(row for row in cohort['items'] if row['fund_code']==code)
    period = '2025-12-31' if product==142 else '2026-06-30'
    status = 'not_applicable' if row['found_date'] > period else 'not_disclosed'
    return store.record_disclosure_state(product, code, 'controlled-official-eligibility-fixture',
                                         {'fund_code': code, 'found_date': row['found_date'],
                                          'required_report_period': period,
                                          'directory_version': cohort['directory_version'],
                                          'scope_version': gen.scope_version(row)},
                                         status=status)


@pytest.mark.parametrize('product,kind,selector,code', [
    (142, 'industry-etf-reverse', 'industryCodes', REAL['142'][0]['industryCode']),
    (143, 'concept-etf-reverse', 'conceptCodes', REAL['143'][0]['conceptCode'])])
def test_real_mapping_rows_share_one_version_forward_reverse_and_threshold(db, monkeypatch, product, kind, selector, code):
    assert db
    cohort = directory(monkeypatch)
    stage(product, cohort)
    absence(product, cohort)
    result = gen.materialize(args(kind))
    forward = gen.query(product, {'fundCodes': ['510300'], 'pageSize': 500})
    reverse = gen.query(product, {selector: [code]})
    assert forward['generation'] == reverse['generation'] == result['generation']
    assert forward['dataset_scope'] == {'catalog_fund_count': 2, 'eligible_fund_count': 1,
                                      'undisclosed_fund_count': 0, 'not_applicable_fund_count': 1}
    matching = [row for row in forward['items'] if row.get(selector[:-1]) == code]
    assert reverse['items'] == matching
    assert gen.query(product, {selector: [code], 'minAnnualWeight': 100})['total'] == 0
    with pytest.raises(LookupError):
        gen.query(product, {'fundCodes': ['158008']})  # Known undisclosed is not a successful empty fund.


def test_missing_scope_is_not_zero_and_partial_generation_rolls_back(db, monkeypatch):
    assert db
    cohort = directory(monkeypatch)
    stage(142, cohort)
    with pytest.raises(LookupError, match='lacks'):
        gen.materialize(args('industry-etf-reverse'))
    with db.transaction(read_only=True) as conn:
        assert store._sql(conn, 'SELECT COUNT(*) AS n FROM open_fund_etf_generation_fact', fetch=True)[0]['n'] == 0
    with pytest.raises(LookupError):
        gen.query(142, {'fundCodes': ['510300']})


def test_added_or_removed_etf_invalidates_both_old_selectors(db, monkeypatch):
    assert db
    cohort = directory(monkeypatch)
    stage(142, cohort);absence(142, cohort)
    gen.materialize(args('industry-etf-reverse'))
    directory(monkeypatch, codes=('510300', '159919'))
    for query in ({'fundCodes': ['510300']}, {'industryCodes': [REAL['142'][0]['industryCode']]}):
        with pytest.raises(LookupError, match='changed'):
            gen.query(142, query)


def test_new_corrupted_directory_never_falls_back_to_old_one(db, monkeypatch):
    assert db
    directory(monkeypatch)
    directory(monkeypatch)
    with db.transaction() as conn:
        store._sql(conn, 'UPDATE open_fund_etf_directory SET payload_hash=? WHERE version_id=('
                   'SELECT version_id FROM open_fund_etf_directory ORDER BY recorded_at DESC LIMIT 1)', ('0'*64,))
    with pytest.raises(LookupError, match='corrupted'):
        gen.members()


def test_future_directory_observation_is_rejected(db, monkeypatch):
    assert db
    with pytest.raises(ValueError, match='observation time'):
        directory(monkeypatch, observed_at=time.time()+3600)


def test_any_other_page_missing_fact_invalidates_complete_generation(db, monkeypatch):
    assert db
    cohort=directory(monkeypatch);stage(143, cohort);absence(143, cohort)
    result=gen.materialize(args('concept-etf-reverse'))
    with db.transaction() as conn:
        store._sql(conn, 'DELETE FROM open_fund_etf_generation_fact WHERE generation_id=? AND association_code=?',
                   (result['generation'], REAL['143'][-1]['conceptCode']))
    with pytest.raises(LookupError, match='physically'):
        gen.query(143, {'fundCodes': ['510300'], 'pageNum': 1, 'pageSize': 1})


def test_published_version_has_no_expiry_and_stays_readable(db, monkeypatch):
    assert db
    cohort=directory(monkeypatch, observed_at=1000.0);stage(142,cohort);absence(142,cohort)
    result=gen.materialize(args('industry-etf-reverse'))
    body = gen.query(142, {'industryCodes': [REAL['142'][0]['industryCode']]})
    assert 'valid_until' not in result['quality'] and 'valid_until' not in body
    assert body['generation'] == result['generation'] and body['total'] > 0


def test_latest_unknown_directory_context_or_unreviewed_method_is_not_blessed(db, monkeypatch):
    assert db
    cohort=directory(monkeypatch);stage(142,cohort);absence(142,cohort)
    with db.transaction() as conn:
        store._sql(conn, "UPDATE open_fund_product_batch SET method_version='invented'")
    with pytest.raises(ValueError, match='unreviewed'):
        gen.materialize(args('industry-etf-reverse'))


def test_all_verified_non_disclosures_publish_metadata_but_not_financial_empty(db, monkeypatch):
    assert db
    cohort=directory(monkeypatch)
    absence(142,cohort,code='510300');absence(142,cohort)
    from app.open_data.product_outcomes import NotDisclosedError  # pylint: disable=import-outside-toplevel
    with pytest.raises(NotDisclosedError):
        gen.materialize(args('industry-etf-reverse'))
    with pytest.raises(LookupError):
        gen.query(142, {'industryCodes': [REAL['142'][0]['industryCode']]})


def test_added_etf_reuses_unchanged_member_checkpoints_not_whole_catalog_research(db, monkeypatch):
    assert db
    old=directory(monkeypatch);stage(142,old);absence(142,old)
    first=gen.materialize(args('industry-etf-reverse'))
    new=directory(monkeypatch,codes=('510300','158008','159919'))
    absence(142,new,code='159919')
    second=gen.materialize(args('industry-etf-reverse'))
    assert first['generation'] != second['generation']
    body=gen.query(142,{'fundCodes':['510300']})
    assert body['dataset_scope']['catalog_fund_count']==3 and body['total']==29


def test_false_setup_date_cannot_explain_a_missing_real_etf(db, monkeypatch):
    assert db
    cohort=directory(monkeypatch);stage(142,cohort);absence(142,cohort)
    with db.transaction() as conn:
        entries=store._sql(conn,'SELECT version_id,evidence_json FROM open_fund_disclosure_eligibility',fetch=True)
        value=json.loads(entries[0]['evidence_json']);value['found_date']='2010-01-01'
        raw=gen._encode(value)
        store._sql(conn,'UPDATE open_fund_disclosure_eligibility SET evidence_json=?,evidence_sha256=? WHERE version_id=?',
                   (raw,hashlib.sha256(raw.encode()).hexdigest(),entries[0]['version_id']))
    with pytest.raises(LookupError,match='setup date'):
        gen.materialize(args('industry-etf-reverse'))


def test_unchanged_source_versions_reuse_and_do_not_rewrite_all_etf_facts(db, monkeypatch):
    assert db
    cohort=directory(monkeypatch);stage(143,cohort);absence(143,cohort)
    first=gen.materialize(args('concept-etf-reverse'))
    second=gen.materialize(args('concept-etf-reverse'))
    assert second['reused'] is True and second['generation']==first['generation']
    with db.transaction(read_only=True) as conn:
        assert store._sql(conn,'SELECT COUNT(*) AS n FROM open_fund_etf_generation_fact',fetch=True)[0]['n']==552
    with db.transaction() as conn:
        store._sql(conn,'DELETE FROM open_fund_etf_generation_fact WHERE generation_id=? AND association_code=?',
                   (first['generation'],REAL['143'][0]['conceptCode']))
    healed=gen.materialize(args('concept-etf-reverse'))
    assert healed['reused'] is False and healed['generation']!=first['generation']
    assert gen.query(143,{'fundCodes':['510300']})['total']==552


def test_published_and_staging_fact_retention_is_bounded_without_deleting_pdf_lineage(db, monkeypatch):
    assert db
    cohort=directory(monkeypatch)
    for _ in range(4):
        stage(142,cohort);absence(142,cohort)
        gen.prune_staging(142,'510300')
        gen.materialize(args('industry-etf-reverse'))
    with db.transaction(read_only=True) as conn:
        assert store._sql(conn,'SELECT COUNT(*) AS n FROM open_fund_etf_generation',fetch=True)[0]['n']==2
        assert store._sql(conn,'SELECT COUNT(*) AS n FROM open_fund_etf_generation_fact',fetch=True)[0]['n']==58
        assert store._sql(conn,"SELECT COUNT(*) AS n FROM open_fund_product_batch WHERE status='archived'",fetch=True)[0]['n']==2
        assert store._sql(conn,'SELECT COUNT(*) AS n FROM open_fund_product_fact',fetch=True)[0]['n']==58


@pytest.mark.parametrize('column,value',[('association_code','CLS999999999'),('fund_code','999998'),('annual_weight',99)])
def test_indexes_cannot_move_a_published_payload_outside_the_where_clause(db,monkeypatch,column,value):
    assert db
    cohort=directory(monkeypatch);stage(143,cohort);absence(143,cohort)
    result=gen.materialize(args('concept-etf-reverse'))
    with pytest.raises(Exception,match='etf_fact_immutable'):
        with db.transaction() as conn:
            store._sql(conn,'UPDATE open_fund_etf_generation_fact SET '+column+'=? WHERE generation_id=?',
                       (value,result['generation']))
    code=REAL['143'][0]['conceptCode']
    assert gen.query(143,{'conceptCodes':[code]})['total']==1


def test_guardless_database_cannot_publish_a_v2_generation(db,monkeypatch):
    assert db
    cohort=directory(monkeypatch);stage(142,cohort);absence(142,cohort)
    with db.transaction() as conn:
        store._sql(conn,'DROP TRIGGER etf_fact_bound_insert')
    with pytest.raises(ValueError,match='guard'):
        gen.materialize(args('industry-etf-reverse'))


def test_page_byte_budget_rejected_before_fact_fetchall(db,monkeypatch):
    assert db
    cohort=directory(monkeypatch)
    rows=[{**row,'fundName':'x'*750000} for row in REAL['142'][:3]]
    stage(142,cohort,rows=rows);absence(142,cohort)
    gen.materialize(args('industry-etf-reverse'))
    original=store._sql
    def spy(conn,sql,params=(),**kwargs):
        if 'SELECT payload_json,payload_hash FROM open_fund_etf_generation_fact' in sql:
            raise AssertionError('Oversized page payload must not be fetched')
        return original(conn,sql,params,**kwargs)
    monkeypatch.setattr(store,'_sql',spy)
    with pytest.raises(LookupError,match='byte budget'):
        gen.query(142,{'fundCodes':['510300'],'pageSize':3})
