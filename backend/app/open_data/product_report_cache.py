"""Bounded offline cache of validated immutable issuer-report parsing, not a live source."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

VERSION = 'issuer-table-parser-v3'


def load_report(metadata, fund_code, *, parser, periodic=False):
    """The caller still obtains a fresh official announcement list on every collection.

    Caching an immutable announcement avoids re-downloading/re-parsing hundreds
    of thousands of pages on each recurring sweep. A changed ID/title/parser
    version is a different cache key. The read service never imports this path.
    """
    if metadata.get('FUNDCODE') != fund_code or not re.fullmatch(r'AN[0-9]{18}', metadata.get('ID', '')):
        raise ValueError('Exact report-cache identity is required')
    identity = [VERSION, metadata, fund_code, periodic]
    key = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    directory = Path(os.getenv('IFUND_OPEN_REPORT_CACHE_DIR', '/runtime/ifund-report-cache'))
    path = directory/(key+'.json')
    try:
        if path.stat().st_size <= 8000000:
            payload = json.loads(path.read_text(encoding='utf-8'))
            encoded = json.dumps(payload['report'], ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
            if (payload.get('version') == VERSION and payload.get('identity') == key
                    and payload.get('sha256') == hashlib.sha256(encoded).hexdigest()):
                os.utime(path, None)
                return payload['report']
    except (OSError, ValueError, KeyError, TypeError):
        pass  # Offline collector may heal an absent/corrupt cache using the fixed original source.
    from curl_cffi import requests  # pylint: disable=import-outside-toplevel
    response = requests.get('https://pdf.dfcfw.com/pdf/H2_'+metadata['ID']+'_1.pdf',
                            impersonate='chrome', timeout=60, allow_redirects=False)
    response.raise_for_status()
    report = parser(response.content, metadata, fund_code, periodic=periodic)
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    payload = {'version': VERSION, 'identity': key, 'sha256': hashlib.sha256(encoded).hexdigest(), 'report': report}
    # Cache failures never become a successful fact without original parsing.
    # The cache is optional: read-only collectors still use the verified source.
    try:
        limit = int(os.getenv('IFUND_OPEN_REPORT_CACHE_MAX_BYTES', str(128*1024*1024)))
        if not 8000000 <= limit <= 16*1024*1024*1024:
            raise ValueError('Report cache capacity must be 8MB..16GB')
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory/('.'+uuid.uuid4().hex+'.tmp')
        temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
        temporary.replace(path)
        _prune(directory, limit)
    except OSError:
        pass
    return report


def _prune(directory, limit):
    files, total = [], 0
    for path in directory.glob('*.json'):
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        files.append((stat.st_mtime, path, stat.st_size))
        total += stat.st_size
    for observed, path, size in sorted(files):
        if total <= limit and observed > time.time()-90*86400:
            break
        path.unlink(missing_ok=True)
        total -= size
