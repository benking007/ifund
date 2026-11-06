"""Complete bounded production universes; no silent omission of missing roles."""
from __future__ import annotations

import os
import math
import time
from datetime import date
import json
from pathlib import Path

from app import db as database
from . import product_store as store

KINDS = ('active_funds', 'exchange_funds', 'sse_exchange_funds', 'unit_nav_funds',
         'money_funds', 'stock_holding_funds', 'unique_current_managers', 'benchmark_verified_funds',
         'fof_funds', 'unique_benchmarks', 'verified_etf_funds')


def members(kind):
    """Generate argument-safe per-scope inputs from actual governed identities."""
    if kind not in KINDS:
        raise ValueError('Unknown collection universe')
    if kind == 'verified_etf_funds':
        from .product_etf_generation import members as etf_members  # pylint: disable=import-outside-toplevel
        return etf_members()
    if kind == 'unique_benchmarks':
        result = members('benchmark_verified_funds')
        unique = {row['benchmark_ts_code']: {'scope_id': row['benchmark_ts_code'],
                  'ts_code': row['benchmark_ts_code'], 'benchmark_code': row['benchmark_code']}
                  for row in result['members']}
        return {**result, 'universe': kind, 'members': list(unique.values())}
    if kind == 'unique_current_managers':
        return _managers()
    with database.get_db().transaction(read_only=True) as conn:
        # pylint: disable=protected-access
        rows = store._sql(conn, "SELECT e.fund_code,e.ts_code,e.market,e.benchmark,f.type "
                          "FROM fund_basic_ext e LEFT JOIN funds f ON f.code=e.fund_code "
                          "WHERE e.status='L' AND e.source='tushare' "
                          "ORDER BY e.fund_code LIMIT 100001", fetch=True)
        # pylint: enable=protected-access
    if not rows or len(rows) > 100000:
        raise ValueError('A bounded complete canonical active fund catalog is required')
    allocation = _allocations() if kind == 'stock_holding_funds' else {}
    result, excluded, unknown = [], 0, 0
    for row in rows:
        if not row.get('ts_code') or row['ts_code'].split('.')[0] != row['fund_code']:
            raise ValueError('Catalog contains an unverified source role')
        money = isinstance(row['type'], str) and row['type'].startswith(('货币型', '货币市场型'))
        exchange = row['market'] == 'E' and row['ts_code'].endswith(('.SH', '.SZ'))
        if kind == 'money_funds' and not money:
            continue
        if kind == 'fof_funds' and not (isinstance(row['type'], str) and 'FOF' in row['type']):
            continue
        if kind in {'unit_nav_funds', 'stock_holding_funds', 'benchmark_verified_funds'} and money:
            continue
        if kind == 'stock_holding_funds':
            eligible, proven = _stock_evidence(allocation.get(row['fund_code'], {}))
            if not eligible:
                unknown += int(not proven)
                excluded += 1
                continue
        if kind in {'exchange_funds', 'sse_exchange_funds'} and not exchange:
            continue
        if kind == 'sse_exchange_funds' and not row['ts_code'].endswith('.SH'):
            continue
        result.append({'scope_id': row['fund_code'], 'fund_code': row['fund_code'], 'ts_code': row['ts_code'],
                       **({'fund_benchmark_expression': row['benchmark']} if row.get('benchmark') else {})})
    metadata = {'scope_definition': 'active_canonical_fund_master',
                'canonical_population': len(rows), 'total': len(rows)}
    if kind == 'benchmark_verified_funds':
        eligible = len(result)
        result = _benchmarks(result)
        metadata.update(scope_definition='explicitly_configured_verified_benchmark_subset',
                        eligible_unit_nav_funds=eligible, unconfigured_funds=eligible-len(result),
                        benchmark_unconfigured=eligible-len(result),
                        coverage='configured_subset_not_all_funds')
    if kind == 'stock_holding_funds':
        metadata.update(scope_definition='actual_strictly_positive_reported_stock_allocation_subset',
                        excluded_or_unknown_stock_allocation=excluded, unknown_count=unknown,
                        coverage='verified_eligible_subset')
    metadata.update(included_count=len(result), excluded_count=len(rows)-len(result))
    metadata.setdefault('unknown_count',
                        sum(not isinstance(row['type'], str) or not row['type'] for row in rows))
    return {'complete': True, 'members': result, 'universe': kind,
            'source': 'canonical-active-tushare-fund-master', **metadata}


def _allocations():
    """Only the latest physically complete actual 259 disclosure, never detail trade_date."""
    with database.get_db().transaction(read_only=True) as conn:
        # pylint: disable=protected-access
        rows = store._sql(conn, 'WITH latest AS (SELECT *,ROW_NUMBER() OVER (PARTITION BY scope_key '
                          'ORDER BY recorded_at DESC,batch_id DESC) AS rn FROM open_fund_product_batch '
                          "WHERE product_id=259 AND scope_kind='fund' AND status='ready') "
                          'SELECT b.scope_key,b.row_count,b.valid_until,b.window_end,f.payload_json '
                          'FROM latest b LEFT JOIN open_fund_product_fact f ON f.batch_id=b.batch_id '
                          'WHERE b.rn=1 LIMIT 100001', fetch=True)
        # pylint: enable=protected-access
    if len(rows) > 100000:
        raise ValueError('Allocation eligibility population exceeds bound')
    grouped = {}
    for row in rows:
        grouped.setdefault(row['scope_key'], []).append(row)
    result = {}
    for code, physical in grouped.items():
        row = physical[0]
        if len(physical) != 1 or row['row_count'] != 1 or row['valid_until'] <= time.time():
            continue
        try:
            fact = json.loads(row['payload_json'])
        except (ValueError, TypeError):
            continue
        if fact.get('fundCode') == code:
            result[code] = {'position_stock': fact.get('stockRatio'), 'trade_date': str(row['window_end'])}
    return result


def _stock_evidence(row):
    value = row.get('position_stock')
    numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
    if not numeric or not math.isfinite(value) or not 0 <= value <= 100:
        return False, False
    try:
        observed = date.fromisoformat(str(row.get('trade_date')))
    except ValueError:
        return False, False
    if observed > date.today():
        return False, False
    return value > 0, True


def _managers():
    with database.get_db().transaction(read_only=True) as conn:
        # pylint: disable=protected-access
        latest = store._sql(conn, 'SELECT snapshot_id,valid_until,declared_count '
                           'FROM open_fund_manager_directory_batch ORDER BY recorded_at DESC LIMIT 1', fetch=True)
        if not latest:
            raise ValueError('Complete manager directory must be collected first')
        rows = store._sql(conn, 'SELECT manager_id,manager_name FROM open_fund_manager_directory '
                         'WHERE snapshot_id=? ORDER BY manager_id', (latest[0]['snapshot_id'],), fetch=True)
        # pylint: enable=protected-access
        if len(rows) != latest[0]['declared_count']:
            raise ValueError('Manager directory physical completeness failed')
        names = {}
        for row in rows:
            names.setdefault(row['manager_name'], []).append(row['manager_id'])
        result = [{'scope_id': ids[0], 'manager_id': ids[0], 'manager_name': name}
                  for name, ids in names.items() if len(ids) == 1]
        # Freshness and exact identity are checked again per collection.
        if result:
            store.manager_identity(result[0]['manager_name'], conn=conn)
    return {'complete': True, 'members': result, 'universe': 'unique_current_managers',
            'ambiguous_names_rejected': sum(len(ids) > 1 for ids in names.values())}


def _benchmarks(rows):
    configured = os.getenv('IFUND_OPEN_BENCHMARK_ROLES_FILE', '')
    if not configured:
        raise ValueError('Explicit approved per-fund benchmark-role file is required')
    source = Path(configured)
    if source.stat().st_size > 8000000:
        raise ValueError('Benchmark role configuration exceeds bound')
    roles = json.loads(source.read_text(encoding='utf-8'))
    result = []
    for row in rows:
        role = roles.get(row['fund_code'])
        if role is None:
            continue
        if not isinstance(role, dict) or role.get('verified') is not True:
            raise ValueError('Unverified benchmark role')
        expression = role.get('fund_benchmark_expression')
        if (not role.get('evidence_uri') or role.get('ts_code', '').split('.')[0] != role.get('code')
                or not expression or row.get('fund_benchmark_expression') != expression):
            raise ValueError('Unverified benchmark role')
        result.append({**row, 'benchmark_code': role['code'], 'benchmark_ts_code': role['ts_code']})
    if not result:
        raise ValueError('No actual benchmark role has been configured')
    return result
