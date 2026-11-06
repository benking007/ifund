"""Operator-only source PAT bootstrap. Never exposes a public administration API."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets

import bcrypt

from app import db as database
from .product_store import _sql


def create(*, user_id=None, service_user=None, name, output):
    """Write a new secret once to a newly created 0600 file; DB holds only its hash."""
    if (user_id is None) == (service_user is None):
        raise ValueError('Exactly one explicit owner choice is required')
    if service_user and not re.fullmatch(r'svc-[A-Za-z0-9_.-]{1,64}', service_user):
        raise ValueError('Dedicated service username must begin svc-')
    if user_id is not None and (isinstance(user_id, bool) or user_id <= 0):
        raise ValueError('Positive explicit user ID required')
    if not name or len(name) > 100:
        raise ValueError('Bounded token label required')
    path = Path(output)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    raw = 'ifd_'+secrets.token_urlsafe(32)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as destination:
            with database.get_db().transaction() as conn:
                if service_user:
                    if _sql(conn, 'SELECT id FROM users WHERE username=?', (service_user,), fetch=True):
                        raise ValueError('Dedicated service username already exists; use its explicit user ID')
                    password = bcrypt.hashpw(secrets.token_urlsafe(48).encode(), bcrypt.gensalt()).decode()
                    _sql(conn, 'INSERT INTO users(username,hashed_password) VALUES(?,?)', (service_user, password))
                    user_id = _sql(conn, 'SELECT id FROM users WHERE username=?', (service_user,), fetch=True)[0]['id']
                elif not _sql(conn, 'SELECT id FROM users WHERE id=?', (user_id,), fetch=True):
                    raise ValueError('Explicit token owner does not exist')
                digest = hashlib.sha256(raw.encode()).hexdigest()
                _sql(conn, 'INSERT INTO api_tokens(user_id,name,token_hash,token_prefix) VALUES(?,?,?,?)',
                     (user_id, name, digest, raw[:12]))
                token_id = _sql(conn, 'SELECT id FROM api_tokens WHERE token_hash=?', (digest,), fetch=True)[0]['id']
                destination.write(raw+'\n')
                destination.flush()
                os.fsync(destination.fileno())
        return {'status': 'created', 'user_id': user_id, 'token_id': token_id, 'output_file': str(path)}
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def revoke(*, user_id, token_id):
    """Owner-scoped idempotent revocation; no token lookup or secret recovery."""
    if min(user_id, token_id) <= 0:
        raise ValueError('Positive owner and token IDs required')
    with database.get_db().transaction() as conn:
        if not _sql(conn, 'SELECT id FROM api_tokens WHERE id=? AND user_id=?', (token_id, user_id), fetch=True):
            raise ValueError('Token does not belong to this owner')
        _sql(conn, 'UPDATE api_tokens SET revoked=1 WHERE id=? AND user_id=?', (token_id, user_id))
    return {'status': 'revoked', 'user_id': user_id, 'token_id': token_id}


def main(argv=None):
    """Keep successful diagnostics metadata-only and failed diagnostics secret-free."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    issuance = commands.add_parser('create')
    owners = issuance.add_mutually_exclusive_group(required=True)
    owners.add_argument('--user-id', type=int)
    owners.add_argument('--create-service-user', metavar='svc-NAME')
    issuance.add_argument('--name', required=True)
    issuance.add_argument('--output', required=True)
    deletion = commands.add_parser('revoke')
    deletion.add_argument('--user-id', type=int, required=True)
    deletion.add_argument('--token-id', type=int, required=True)
    args = parser.parse_args(argv)
    try:
        result = (create(user_id=args.user_id, service_user=args.create_service_user, name=args.name,
                         output=args.output) if args.command == 'create' else
                  revoke(user_id=args.user_id, token_id=args.token_id))
    except Exception as exc:  # pylint: disable=broad-exception-caught
        print(json.dumps({'status': 'failed', 'error_type': type(exc).__name__}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
