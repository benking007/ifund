"""Database-enforced row binding and immutable published ETF facts."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid

UPDATE_NAME = 'etf_fact_immutable_update'
INSERT_NAME = 'etf_fact_bound_insert'


def install(conn, sql):
    """Explicit migration role requires TRIGGER privilege; reader/collector do not."""
    table = 'open_fund_etf_generation_fact'
    code = "COALESCE(REPLACE(CAST(JSON_EXTRACT(NEW.payload_json,'$.fundCode') AS CHAR),'\"',''),'')"
    association = ("COALESCE(REPLACE(CAST(JSON_EXTRACT(NEW.payload_json,'$.industryCode') AS CHAR),'\"',''),"
                   "REPLACE(CAST(JSON_EXTRACT(NEW.payload_json,'$.conceptCode') AS CHAR),'\"',''),'')")
    weight = ("CAST(COALESCE(JSON_EXTRACT(NEW.payload_json,'$.holdingAssetAnnual'),"
              "JSON_EXTRACT(NEW.payload_json,'$.holdingWeightAnnual'),-999999) AS DECIMAL(36,12))")
    binding = 'JSON_VALID(NEW.payload_json) AND NEW.fund_code='+code+' AND NEW.association_code='+association
    binding += ' AND NEW.annual_weight='+weight
    published = 'EXISTS(SELECT 1 FROM open_fund_etf_generation WHERE generation_id=NEW.generation_id)'
    if isinstance(conn, sqlite3.Connection):
        sql(conn, 'CREATE TRIGGER IF NOT EXISTS '+UPDATE_NAME+' BEFORE UPDATE ON '+table+
            " BEGIN SELECT RAISE(ABORT,'etf_fact_immutable'); END")
        sql(conn, 'CREATE TRIGGER IF NOT EXISTS '+INSERT_NAME+' BEFORE INSERT ON '+table+
            " BEGIN SELECT CASE WHEN "+published+" THEN RAISE(ABORT,'etf_fact_published') END;"
            " SELECT CASE WHEN NOT ("+binding+") THEN RAISE(ABORT,'etf_fact_binding') END; END")
    else:
        triggers = {row['TRIGGER_NAME'] for row in sql(conn, 'SELECT TRIGGER_NAME FROM information_schema.triggers '
                    'WHERE TRIGGER_SCHEMA=DATABASE() AND EVENT_OBJECT_TABLE=?', (table,), fetch=True)}
        if UPDATE_NAME not in triggers:
            sql(conn, 'CREATE TRIGGER '+UPDATE_NAME+' BEFORE UPDATE ON '+table+
                " FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='etf_fact_immutable'")
        if INSERT_NAME not in triggers:
            sql(conn, 'CREATE TRIGGER '+INSERT_NAME+' BEFORE INSERT ON '+table+' FOR EACH ROW BEGIN IF '+published+
                " THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='etf_fact_published'; END IF; IF NOT ("+binding+
                ") THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='etf_fact_binding'; END IF; END")


def _rejected(conn, sql, statement, params, label):
    try:
        sql(conn, statement, params)
    except Exception as error:  # pylint: disable=broad-exception-caught
        # Permission/network/syntax failures are NOT evidence that the guard exists.
        args = getattr(error, 'args', ())
        expected = ((isinstance(error, sqlite3.IntegrityError) and label in str(error)) or
                    (args and args[0] == 1644 and label in str(error)))
        if expected:
            return
        raise
    raise ValueError('ETF database immutability guard is not installed')


def prove(conn, sql):
    """A rolled-back savepoint proves guards before any v2 publication, without reader DDL privileges."""
    key = uuid.uuid4().hex
    raw = json.dumps({'fundCode': '999999', 'industryCode': '710000', 'holdingAssetAnnual': 0})
    record = (key, '999999', '710000', 0, raw, hashlib.sha256(raw.encode()).hexdigest())
    statement = 'INSERT INTO open_fund_etf_generation_fact VALUES(?,?,?,?,?,?)'
    sql(conn, 'SAVEPOINT etf_integrity_probe')
    try:
        _rejected(conn, sql, statement, (key, '999998', *record[2:]), 'etf_fact_binding')
        sql(conn, statement, record)
        _rejected(conn, sql, 'UPDATE open_fund_etf_generation_fact SET association_code=? WHERE generation_id=?',
                  ('710001', key), 'etf_fact_immutable')
        sql(conn, 'INSERT INTO open_fund_etf_generation VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (key, 142, 'a'*64, 'b'*32, 0, 1, 1, 1, 0, 0, 1, len(raw), 'guard-probe-rolled-back', 'c'*64))
        other = json.dumps({'fundCode': '999998', 'industryCode': '710000', 'holdingAssetAnnual': 0})
        _rejected(conn, sql, statement, (key, '999998', '710000', 0, other,
                  hashlib.sha256(other.encode()).hexdigest()), 'etf_fact_published')
    finally:
        sql(conn, 'ROLLBACK TO SAVEPOINT etf_integrity_probe')
        sql(conn, 'RELEASE SAVEPOINT etf_integrity_probe')
