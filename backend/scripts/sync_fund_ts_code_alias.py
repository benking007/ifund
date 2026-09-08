#!/usr/bin/env python3
"""从已落库的 Tushare fund_basic 歧义证据幂等生成 ts_code alias。"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)

from app.fund_nav import ts_code_map  # pylint: disable=wrong-import-position

DEFAULT_EVIDENCE = (
    BACKEND_DIR / "logs" / "governance" / "fund-ts-code-alias-latest.json"
)


def atomic_json(path: Path, payload: dict) -> None:
    """原子写入迁移证据。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    """执行建表和回填，不调用 Tushare、不改 fund_ts_code_map。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-path", type=Path, default=DEFAULT_EVIDENCE)
    args = parser.parse_args(argv)
    if os.getenv("DB_BACKEND", "").strip().lower() != "mysql":
        print("sync_fund_ts_code_alias 仅允许 DB_BACKEND=mysql", file=sys.stderr)
        return 2
    result = ts_code_map.sync_aliases_from_stored_evidence()
    payload = {
        "ok": True,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "tushare_calls": 0,
        **result,
    }
    atomic_json(args.evidence_path.resolve(), payload)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
