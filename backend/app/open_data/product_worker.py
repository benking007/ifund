"""Explicit offline fund-product migration, collection and attested ingest CLI."""
from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import re

from . import product_collectors as collectors
from . import product_calculations as calculations
from . import product_eastmoney as eastmoney
from . import product_store as store
from . import product_additional as additional
from .product_contracts import DATE_FIELDS, day
from .product_outcomes import NotDisclosedError
from . import product_base, product_etf_mapping, product_etf_generation


def _window(records, product, start, end):
    field = DATE_FIELDS.get(product)
    if not field:
        return records
    result = []
    for record in records:
        if not record["data"].get(field):
            if product in {240, 270, 271}:
                result.append(record)
                continue
            raise ValueError("A historical product requires its actual reference date")
        if start <= day(record["data"][field]) <= end:
            result.append(record)
    return result


def _scope(args):
    if args.fund_code:
        return "fund", args.fund_code
    if args.benchmark_code:
        return "benchmark", args.benchmark_code
    return "global", "*"


def _index_rows(raw, ts_code, benchmark_code):
    result = []
    for row in raw:
        if row.get("ts_code") != ts_code:
            raise ValueError("Benchmark identity mismatch")
        when = day(row["trade_date"])
        result.append({"record_key": ts_code+":"+when, "data": {
            "benchmarkIndexCode": benchmark_code, "date": when,
            **{target: collectors.number(row.get(source)) for source, target in
               (("close", "closePrice"), ("change", "chg"), ("pct_chg", "changePct"))}}})
    return result


def _computed(args, start, end):
    from app import db as database  # pylint: disable=import-outside-toplevel
    code = args.fund_code
    master = database.select_one("fund_basic_ext", {"fund_code": f"eq.{code}"})
    if not code or not master or master["ts_code"] != args.ts_code:
        raise ValueError("A verified canonical fund master is required")
    if args.kind == "benchmark-components":
        records = calculations.benchmark_rows(master.get("benchmark"), code)
        return (240, records, {"ts_code": args.ts_code, "benchmark": master.get("benchmark")},
                "explicit-weighted-current-benchmark-v1")
    if args.kind == "manager-history":
        raw = database.select("fund_manager_tenure", {"fund_code": f"eq.{code}", "limit": "1000"})
        if len(raw) >= 1000:
            raise ValueError("Manager observation bound reached")
        return 252, calculations.manager_history_rows(raw, code), raw, "reported-team-tenure-snapshot-v1"
    params = {"ts_code": args.ts_code, "start_date": start.replace("-", ""), "end_date": end.replace("-", "")}
    nav_raw = collectors.collect_tushare("fund_nav", params)
    navs = []
    for row in nav_raw:
        if row.get("ts_code") != args.ts_code:
            raise ValueError("NAV source role mismatch")
        navs.append({"fundCode": code, "date": day(row["nav_date"]),
                     "unitNav": row.get("unit_nav"), "adjNav": row.get("adj_nav")})
    if args.kind == "premium":
        if master.get("market") != "E" or not args.ts_code.endswith((".SH", ".SZ")):
            raise ValueError("An exchange-fund role is required")
        raw = collectors.collect_tushare("fund_daily", params)
        prices = [{"fundCode": code, "date": day(row["trade_date"]), "closePrice": row["close"]}
                  for row in raw if row.get("ts_code") == args.ts_code]
        if len(prices) != len(raw):
            raise ValueError("Market close role mismatch")
        records = calculations.premium_rows(prices, navs, code)
        return 272, records, {"nav": nav_raw, "close": raw}, "same-day-close-unit-nav-premium-v1"
    if not args.benchmark_code or not args.benchmark_ts_code:
        raise ValueError("An explicit benchmark code and canonical role are required")
    raw = collectors.collect_tushare("index_daily", {**params, "ts_code": args.benchmark_ts_code})
    quotes = [{"indexCode": args.benchmark_code, "date": day(row["trade_date"]), "closePrice": row["close"]}
              for row in raw if row.get("ts_code") == args.benchmark_ts_code]
    if len(quotes) != len(raw):
        raise ValueError("Benchmark source role mismatch")
    records = calculations.correlation_rows(navs, quotes, code, args.benchmark_code)
    return 255, records, {"adj_nav": nav_raw, "benchmark": raw}, "aligned-daily-return-pearson-calendar-v1"


def collect(args):  # pylint: disable=too-many-branches,too-many-return-statements,too-many-statements
    """One bounded collector invocation; an error never marks a window ready."""
    from . import product_reports as reports  # pylint: disable=import-outside-toplevel
    if args.kind == 'etf-directory':
        return product_etf_generation.sync_directory(args)
    if args.kind in product_etf_generation.KINDS:
        return product_etf_generation.materialize(args)
    if args.kind in product_etf_mapping.KINDS:
        return product_etf_mapping.collect(args)
    if args.kind in product_base.KINDS:
        return product_base.collect(args)
    if args.kind == "purchase-status-all":
        return additional.collect_purchase_all(args)
    if args.kind in reports.KINDS:
        return reports.collect(args)
    if args.kind in {"manager-history", "manager-history-global"}:
        from . import product_manager_history as history  # pylint: disable=import-outside-toplevel
        return history.collect_scope(args) if args.kind == "manager-history" else history.materialize(args)
    if args.kind == "fee-history":
        from .product_fee_history import collect as collect_fees  # pylint: disable=import-outside-toplevel
        return collect_fees(args)
    if args.kind == "awards":
        from .product_awards import collect as collect_awards  # pylint: disable=import-outside-toplevel
        return collect_awards(args)
    if args.kind in {"concept-exposure", "concept-reverse"}:
        from . import product_concepts as concepts  # pylint: disable=import-outside-toplevel
        return concepts.collect_scope(args) if args.kind == "concept-exposure" else concepts.materialize(args)
    if args.kind in additional.KINDS:
        return additional.ingest_collection(args)
    start, end = day(args.start), day(args.end)
    if args.fund_code and not re.fullmatch(r"[0-9]{6}", args.fund_code):
        raise ValueError("Fund code must be six digits")
    context = {"fund_code": args.fund_code, "ts_code": args.ts_code, "start_date": start, "end_date": end}
    available = None
    if args.kind in {"asset-allocation", "financial-indicators"}:
        product, records, raw, method = eastmoney.collect(args.kind, args.fund_code, start, end)
        if not records:
            raise ValueError("No actual fund report observations")
        source, uri = "eastmoney:fund-disclosure", "https://fundf10.eastmoney.com"
        if product == 259:
            start = end = records[0]["source_report_period"]
    elif args.kind in {"premium", "correlations", "manager-history", "benchmark-components"}:
        product, records, raw, method = _computed(args, start, end)
        source = {252: "ifund:reported-manager", 240: "tushare:fund-basic"}.get(product, "tushare:fund-nav-calculation")
        uri = "https://fund.eastmoney.com" if product == 252 else "https://tushare.pro"
        if product in {252, 255} and records:
            start = min(record["data"]["date"] for record in records)
            end = max(record["data"]["date"] for record in records)
        if not records:
            raise ValueError("A computed product requires actual source observations")
    elif args.kind in {"money-income", "announcements"}:
        if not args.fund_code:
            raise ValueError("A fund code is required")
        raw = collectors.collect_akshare(args.kind, args.fund_code)
        product = 247 if args.kind == "money-income" else 223
        records = (collectors.money_rows if product == 247 else collectors.announcement_rows)(raw, args.fund_code)
        available = ({"fundCode", "date", "avgDailyProfitPer10k", "avg7dYieldAnn"} if product == 247 else
                     {"fundCode", "fundName", "date", "title", "announcementSource"})
        source, uri = "akshare:eastmoney", "https://akshare.akfamily.xyz/data/fund/fund_public.html"
        method = "money-income-v1" if product == 247 else "announcement-report-dividend-personnel-v1"
    elif args.kind in {"managers", "margin", "pcf-components", "index-quotes"}:
        if args.kind != "index-quotes" and not args.fund_code:
            raise ValueError("A canonical fund code is required")
        if not args.ts_code or (args.fund_code and args.ts_code.split(".")[0] != args.fund_code):
            raise ValueError("An explicit matching canonical ts_code is required")
        if args.fund_code:
            from app import db as database  # pylint: disable=import-outside-toplevel
            master = database.select_one("fund_basic_ext", {"fund_code": f"eq.{args.fund_code}"})
            if not master or master["ts_code"] != args.ts_code:
                raise ValueError("The source master must verify this canonical fund role")
            if args.kind in {"margin", "pcf-components"} and (master.get("market") != "E"
                                                              or not args.ts_code.endswith((".SH", ".SZ"))):
                raise ValueError("An exchange-fund role is required")
        methods = {"managers": "fund_manager", "margin": "margin_detail", "pcf-components":
                   "etf_sh_cons" if args.ts_code.endswith(".SH") else "etf_sz_cons", "index-quotes": "index_daily"}
        params = {"ts_code": args.ts_code}
        if args.kind != "managers":
            params.update(start_date=start.replace("-", ""), end_date=end.replace("-", ""))
        raw = collectors.collect_tushare(methods[args.kind], params)
        if args.kind == "managers":
            product, records = 239, collectors.manager_rows(raw, args.fund_code, args.ts_code)
        elif args.kind == "margin":
            product, records = 231, collectors.margin_rows(raw, args.fund_code, args.ts_code)
        elif args.kind == "index-quotes":
            if not args.benchmark_code:
                raise ValueError("A benchmark selector is required")
            product, records = 254, _index_rows(raw, args.ts_code, args.benchmark_code)
        else:
            headers = json.loads(Path(args.pcf_headers).read_text(encoding="utf-8")) if args.pcf_headers else {}
            product, records = 271, collectors.pcf_component_rows(raw, args.fund_code, args.ts_code, headers)
            raw = {"components": raw, "header_publication_evidence": headers}
        source, uri, method = "tushare:"+methods[args.kind], "https://tushare.pro", methods[args.kind]+"-reviewed-v1"
    elif args.kind == "configured-provider":
        if not args.manifest or args.product is None:
            raise ValueError("An approved manifest and product ID are required")
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        product = args.product
        raw = collectors.configured_provider(manifest, context)
        records = collectors.mapped_rows(product, raw, manifest, context)
        source, uri = manifest["source_name"], manifest["endpoint"]
        method = manifest["method_version"]+":"+collectors.evidence(manifest)[:16]
        available = set(manifest["field_map"])
    else:
        raise ValueError("Unknown collector")
    return store.ingest(product, _window(records, product, start, end),
                        scope_kind=_scope(args)[0], scope_key=_scope(args)[1], window_start=start, window_end=end,
                        source_name=source, method_version=method, evidence_uri=uri,
                        evidence_sha256=collectors.evidence(raw), complete_window=True, available_fields=available)


def main(argv=None):
    """Run migrations explicitly; all data acquisition stays outside requests."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    migration = commands.add_parser("migrate")
    migration.add_argument("--base-schema", action="store_true",
                           help="Explicitly migrate the existing source/auth schema before product tables")
    universe = commands.add_parser("universe")
    from .product_universe import KINDS  # pylint: disable=import-outside-toplevel
    universe.add_argument("--kind", required=True, choices=KINDS)
    upload = commands.add_parser("ingest")
    upload.add_argument("--file", required=True, help="Normalized records, source evidence and verified scope envelope")
    upload.add_argument("--attest-window-complete", action="store_true", required=True)
    fetch = commands.add_parser("collect")
    from .product_reports import KINDS as report_kinds  # pylint: disable=import-outside-toplevel
    fetch.add_argument("--kind", required=True, choices=["money-income", "announcements", "managers", "margin",
                                                       "pcf-components", "index-quotes", "configured-provider",
                                                       "premium", "correlations", "manager-history",
                                                       "asset-allocation", "financial-indicators",
                                                       "benchmark-components", *sorted(additional.KINDS),
                                                       *sorted(report_kinds), "awards", "concept-exposure",
                                                       "concept-reverse", "fee-history", "manager-history-global",
                                                       "purchase-status-all", *product_base.KINDS,
                                                       *product_etf_mapping.KINDS,
                                                       *product_etf_generation.KINDS, "etf-directory"])
    fetch.add_argument("--holding-type", choices=["stock", "bond"], default="stock")
    fetch.add_argument("--bootstrap-days", type=int, default=35)
    fetch.add_argument("--lookback-days", type=int, default=7)
    fetch.add_argument("--product", type=int)
    fetch.add_argument("--fund-code")
    fetch.add_argument("--manager-name")
    fetch.add_argument("--manager-id")
    fetch.add_argument("--benchmark-code")
    fetch.add_argument("--benchmark-ts-code")
    fetch.add_argument("--ts-code")
    fetch.add_argument("--manifest")
    fetch.add_argument("--pcf-headers")
    fetch.add_argument("--start", default="0001-01-01")
    fetch.add_argument("--end", default=date.today().isoformat())
    # Accepted and ignored so older schedulers that still pass it keep working.
    fetch.add_argument("--ttl-seconds", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.command == "migrate":
            if args.base_schema:
                from app import db as database  # pylint: disable=import-outside-toplevel
                database.init_db((Path(__file__).parents[2]/"schema_sqlite.sql").read_text(encoding="utf-8"))
            store.migrate()
            result = {"migration": "open-fund-products-v2-etf-generation", "schemaVersion": 6, "status": "applied"}
        elif args.command == "universe":
            from .product_universe import members  # pylint: disable=import-outside-toplevel
            result = members(args.kind)
        elif args.command == "ingest":
            payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
            payload["complete_window"] = args.attest_window_complete
            result = store.ingest(**payload)
        else:
            result = collect(args)
    except NotDisclosedError as exc:
        print(json.dumps({'status': exc.status, 'quality': {'status': exc.status, 'complete': True,
                          'row_count': 0, 'verified_empty': False, 'reason': exc.reason, 'source': exc.source,
                          'evidence': exc.evidence, 'observed_at': exc.observed_at}}))
        return 0
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Do not print provider payloads/URLs/headers or potential credentials.
        failure = {"status": "failed", "error_type": type(exc).__name__}
        if args.command == 'migrate' and getattr(exc, 'args', ())[:1] == (1419,):
            failure.update(error_code='MYSQL_TRIGGER_BINLOG_DBA_CONFIGURATION_REQUIRED',
                           action='DBA must configure log_bin_trust_function_creators=1 or install reviewed triggers')
        print(json.dumps(failure))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
