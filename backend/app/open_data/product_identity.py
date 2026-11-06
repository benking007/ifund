"""Shared canonical fund identity for independent offline collectors."""
from __future__ import annotations

import re
from datetime import date, timedelta

from app import db as database

from .product_contracts import day


def canonical_master(fund_code):
    """Require an actual governed source role before interpreting an empty response."""
    if not isinstance(fund_code, str) or not re.fullmatch(r'[0-9]{6}', fund_code):
        raise ValueError('A six-digit canonical fund code is required')
    row = database.select_one('fund_basic_ext', {'fund_code': 'eq.'+fund_code})
    if not row or row.get('ts_code', '').split('.')[0] != fund_code or row.get('source') != 'tushare':
        raise ValueError('Canonical fund source role missing')
    return row


def not_yet_reportable(master, as_of, *, semiannual=False):
    """Actual inception after the latest completed reporting period is not a missing feed."""
    end = date.fromisoformat(day(as_of))
    if not master.get('found_date'):
        return None
    inception = date.fromisoformat(day(master['found_date']))
    if semiannual:
        boundary = date(end.year, 7, 1) if end.month >= 7 else date(end.year, 1, 1)
    else:
        boundary = date(end.year, (end.month-1)//3*3+1, 1)
    previous = boundary-timedelta(days=1)
    # At a completed quarter/half-year end, that very date is the latest
    # accounting period even though its publication is still in the future.
    if end.month in ({6, 12} if semiannual else {3, 6, 9, 12}) and (end+timedelta(days=1)).day == 1:
        previous = end
    if inception <= previous:
        return None
    return {'fund_code': master['fund_code'], 'ts_code': master['ts_code'], 'canonical_source': master['source'],
            'found_date': inception.isoformat(), 'latest_completed_report_period': previous.isoformat(),
            'required_report': 'annual-or-semiannual' if semiannual else 'quarterly', 'as_of': end.isoformat()}
