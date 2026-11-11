"""Production-reader watermark presence for legacy base tables, without implicit fetches."""
from __future__ import annotations

import os
import time

from flask import request
from app import db as database
from . import product_store as store

TABLE_KINDS = {'funds': 'base-master', 'fund_types': 'base-categories', 'fund_basic_ext': 'base-master',
               'fund_company': 'base-master', 'fund_nav': 'base-nav', 'fund_details': 'base-returns',
               'fund_manager_tenure': 'base-tenures'}


def require_base(table):
    """A collected watermark must exist for the requested scope; no implicit fetch."""
    if os.getenv('IFUND_OPEN_REQUIRE_BASE_WATERMARK', '0') != '1':
        return
    kind = TABLE_KINDS.get(table)
    suffix = ''
    if table == 'fund_holdings':
        kind, suffix = 'base-holdings', ':'+request.args.get('holding_type', 'stock')
    if table == 'fund_div_split':
        kind = 'base-splits' if request.args.get('event_type') == 'split' else 'base-dividends'
    if not kind:
        return
    selected = (request.args.get('fund_codes') or request.args.get('fund_code') or '*').split(',')
    if kind in {'base-master', 'base-categories'}:
        selected = ['*']
    with database.get_db().transaction(read_only=True) as conn:
        for code in selected:
            # Source write scopes have already been validated by _arguments.
            scopes = [code+suffix] if suffix else [code, '*']
            # pylint: disable=protected-access
            rows = store._sql(conn, 'SELECT observed_at FROM open_fund_base_watermark '
                              'WHERE kind=? AND scope_id IN ('+','.join('?' for _ in scopes)+') '
                              'ORDER BY observed_at DESC,version_id DESC LIMIT 1', (kind, *scopes), fetch=True)
            # pylint: enable=protected-access
            if not rows or rows[0]['observed_at'] > time.time()+60:
                raise LookupError('The newest base-source observation is missing or from the future')
