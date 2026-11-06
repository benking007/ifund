"""Additional fixed-source offline fund products with explicit semantic boundaries."""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
import html
import json
import re
from zoneinfo import ZoneInfo

from app import db as database
from . import product_collectors as collectors
from . import product_eastmoney as eastmoney
from . import product_store as store
from .product_contracts import day
from .product_numbers import numeric
from .product_identity import canonical_master as _master, not_yet_reportable
from .product_outcomes import NotDisclosedError, NotApplicableError

KINDS = {"purchase-status", "holder-ratios", "investment-scope", "company-profile", "peer-returns",
         "industry-exposure", "pcf-header", "quarter-nav", "manager-intervals", "industry-reverse",
         "holding-stock-returns", "manager-directory", "fee-tiers", "performance"}


def assignment(document, name):
    """Decode a JS assignment's literal object without eval or quote rewriting."""
    if len(document.encode()) > 8000000:
        raise ValueError("Source response exceeds its reviewed byte bound")
    match = re.match(r"\s*var\s+"+re.escape(name)+r"\s*=\s*\{", document)
    if not match:
        raise ValueError("Unexpected source assignment")
    offset, values, decoder = match.end(), {}, json.JSONDecoder()
    while True:
        key = re.match(r"\s*([A-Za-z_][A-Za-z_0-9]*)\s*:\s*", document[offset:])
        if not key or key[1] in values:
            raise ValueError("Unexpected or duplicate literal source key")
        offset += key.end()
        value, size = decoder.raw_decode(document[offset:])
        values[key[1]] = value
        offset += size
        token = re.match(r"\s*([,}])\s*", document[offset:])
        if not token:
            raise ValueError("Unexpected executable source content")
        offset += token.end()
        if token[1] == "}":
            if document[offset:].strip() not in ("", ";"):
                raise ValueError("Trailing executable source content")
            return values


def script_value(document, name):
    """Extract exactly one JSON-valued variable from a fixed provider script."""
    matches = list(re.finditer(r"\bvar\s+"+re.escape(name)+r"\s*=\s*", document))
    if len(matches) != 1 or len(document.encode()) > 8000000:
        raise ValueError("Missing or ambiguous source variable")
    value, size = json.JSONDecoder().raw_decode(document[matches[0].end():])
    tail = document[matches[0].end()+size:].lstrip()
    if not tail.startswith(";"):
        raise ValueError("Source variable is not a JSON literal")
    return value


def _record(key, values, **metadata):
    return {"record_key": key, "data": {key: value for key, value in values.items() if value is not None}, **metadata}


def purchase_rows(document, fund_code):
    """Current source status and CNY cap, never an invented change date/type."""
    raw = assignment(document, "reData")
    rows = raw.get("datas")
    if (not isinstance(rows, list) or len(rows) != int(raw.get("record", -1))
            or raw.get("pages") != "1" or raw.get("curpage") != "1"):
        raise ValueError("Purchase source pagination is not complete")
    selected = [row for row in rows if isinstance(row, list) and row and row[0] == fund_code]
    if len(selected) != 1 or len(selected[0]) != 13:
        raise ValueError("Purchase source identity/schema is ambiguous")
    row = selected[0]
    if not isinstance(row[5], str) or not isinstance(row[6], str) or not row[5].strip() or not row[6].strip():
        raise ValueError("Purchase source status is not a string")
    return [_record(fund_code, {"fundCode": fund_code, "fundName": row[1], "subscribeLimit": numeric(row[9]),
                                "remark": "当前申购状态："+row[5]+"；当前赎回状态："+row[6]})]


def collect_purchase_all(args):
    """One full source response and one atomic batch, never N identical downloads."""
    import requests  # pylint: disable=import-outside-toplevel
    with requests.Session() as session:
        response = _get(session, 'https://fund.eastmoney.com/Data/Fund_JJJZ_Data.aspx',
                        params={'t': '8', 'page': '1,50000', 'js': 'reData', 'sort': 'fcode,asc'})
    document = response.content.decode('utf-8')
    raw = assignment(document, 'reData')
    rows = raw.get('datas')
    if (not isinstance(rows, list) or len(rows) != int(raw.get('record', -1))
            or not 1 <= len(rows) <= 50000 or raw.get('pages') != '1' or raw.get('curpage') != '1'):
        raise ValueError('Purchase source full-index pagination is not complete')
    records, seen = [], set()
    for row in rows:
        if not isinstance(row, list) or len(row) != 13 or not re.fullmatch(r'[0-9]{6}', str(row[0])):
            raise ValueError('Purchase full-index security schema changed')
        if row[0] in seen or any(not isinstance(row[i], str) or not row[i].strip() for i in (5, 6)):
            raise ValueError('Duplicate fund or missing purchase status')
        seen.add(row[0])
        records.append(_record(row[0], {'fundCode': row[0], 'fundName': row[1],
                                      'subscribeLimit': numeric(row[9]),
                                      'remark': '当前申购状态：'+row[5]+'；当前赎回状态：'+row[6]}))
    today = date.today().isoformat()
    result = store.ingest(238, records, scope_kind='global', scope_key='*', window_start=today, window_end=today,
                        source_name='eastmoney:complete-current-purchase-index',
                        method_version='one-full-source-snapshot-purchase-status-v2',
                        evidence_uri='https://fund.eastmoney.com/Data/Fund_JJJZ_Data.aspx',
                        evidence_sha256=collectors.evidence(raw), complete_window=True,
                        ttl_seconds=min(args.ttl_seconds, 300))
    result['pruned_old_snapshots'] = store.prune_purchase_snapshots()
    return result


def holder_rows(document, fund_code):
    """Institutional/retail proportions only; ambiguous dates and insiders omitted.

    The source labels its first column publication date but fills report-end
    dates; no event/report date is guessed. '内部' explicitly includes ETF
    feeder holdings, so it must not be presented as employee holdings.
    """
    raw = assignment(document, "apidata")
    parser = eastmoney._Tables()  # pylint: disable=protected-access
    parser.feed(raw["content"])
    expected = ["公告日期", "机构持有比例", "个人持有比例", "内部持有比例", "总份额（亿份）"]
    tables = [table for table in parser.tables if table and table[0] == expected]
    if len(tables) != 1 or not tables[0][1:] or any(len(row) != 5 for row in tables[0][1:]):
        raise ValueError("Holder source table schema changed")
    latest = max(tables[0][1:], key=lambda row: day(row[0]))
    values = {"fundCode": fund_code, "instHoldingRatio": numeric(latest[1]),
              "retailHoldingRatio": numeric(latest[2])}
    if any(value is None or not 0 <= value <= 100 for key, value in values.items() if key != "fundCode"):
        raise ValueError("Invalid holder proportion")
    return [_record(fund_code, values)]


def investment_rows(document, fund_code):
    """Current investment-scope disclosure verbatim, no invented taxonomy/dates."""
    title = re.search(r"<title>(.*?)</title>", document, re.S | re.I)
    if not title or "("+fund_code+")" not in title[1]:
        raise ValueError("Fund profile identity mismatch")
    section = re.search(r"<label[^>]*>\s*投资范围\s*</label>.*?<p>(.*?)</p>", document, re.S)
    if not section:
        raise ValueError("Investment scope disclosure missing")
    text = html.unescape(re.sub(r"<[^>]*>", " ", section[1])).strip()
    if not 10 <= len(text) <= 12000:
        raise ValueError("Unexpected investment scope text")
    return [_record(fund_code, {"fundCode": fund_code, "description": text})]


def pcf_rows(payload, fund_code):
    """SSE disclosed PCF header; previous NAV/cash and effective day stay distinct."""
    rows = payload.get("result")
    if not isinstance(rows, list) or len(rows) != 1 or rows[0].get("TRADE_CODE") != fund_code:
        raise ValueError("SSE PCF header role mismatch")
    raw = rows[0]
    effective, previous = day(raw["TRADING_DAY"]), day(raw["PRE_TRADING_DAY"])
    if previous >= effective:
        raise ValueError("PCF previous day does not precede effective day")
    mapping = {"NAV": "nav", "NAVPERCU": "navPerCreationUnit", "PRE_CASH_COMPONENT": "cashBalance",
               "ESTIMATED_CASH_COMPONENT": "estimatedCashComponent", "CREATION_REDEMPTION_UNIT": "creationUnit",
               "CREATION_LIMIT": "subscribeShareLimit", "REDEMPTION_LIMIT": "redeemShareLimit",
               "CREATION_LIMIT_PER_ACCT": "dailySubscribeLimitPerAccount",
               "REDEMPTION_LIMIT_PER_ACCT": "dailyRedeemLimitPerAccount",
               "NET_CREATION_LIMIT": "netSubscribeShareLimit", "NET_REDEMPTION_LIMIT": "netRedeemShareLimit"}
    values = {"fundCode": fund_code, "fundName": raw.get("FUND_NAME"), "prevTradeDate": previous}
    values.update({target: numeric(raw.get(source)) for source, target in mapping.items()})
    flag = {"是": 1, "否": 2}.get(raw.get("PUBLISH_IOPV"))
    if flag:
        values.update(iopvPublishDesc=flag, isIopvPublished=flag)
    allowed = {"申购和赎回皆允许": (1, 1), "申购和赎回皆不允许": (2, 2), "允许申购，不允许赎回": (1, 2),
               "不允许申购，允许赎回": (2, 1)}.get(raw.get("CREATION_REDEMPTION"))
    if allowed:
        values.update(isSubscribeAllowed=allowed[0], isRedeemAllowed=allowed[1])
    return [_record(fund_code+":"+effective, values, is_etf=True)]


def industry_rows(payload, fund_code):
    """Latest explicitly sourced industry NAV percentages; never infer SW codes."""
    raw = payload.get("Data", {})
    if payload.get("ErrCode") != 0 or raw.get("FundCode") != fund_code:
        raise ValueError("Industry disclosure identity mismatch")
    quarters = raw.get("QuarterInfos")
    if not isinstance(quarters, list) or not quarters:
        raise ValueError("Missing source industry periods")
    latest = max(quarters, key=lambda row: day(row["JZRQ"]))
    period, result = day(latest["JZRQ"]), []
    for row in latest["HYPZInfo"]:
        if row.get("BZDM") != fund_code or day(row["FSRQ"]) != period:
            raise ValueError("Mixed industry disclosure identities")
        ratio = numeric(row.get("ZJZBL"))
        if ratio is None or not 0 <= ratio <= 100:
            raise ValueError("Invalid disclosed industry NAV proportion")
        result.append(_record(fund_code+":"+str(row["HYDM"]), {"fundCode": fund_code,
                      "industryCode": str(row["HYDM"]), "industryName": row["HYMC"], "holdingRatioLatest": ratio}))
    if not result:
        raise ValueError("No disclosed industry facts")
    return result


def _shift_months(when, months):
    year, month = divmod(when.year*12+when.month-1-months, 12)
    return date(year, month+1, min(when.day, calendar.monthrange(year, month+1)[1]))


def peer_rows(document, fund_code):
    """Source-defined peer curve returns over actual common calendar intervals."""
    if script_value(document, "fS_code") != fund_code:
        raise ValueError("Peer curve fund identity mismatch")
    curves = script_value(document, "Data_grandTotal")
    if not isinstance(curves, list) or len(curves) < 2 or curves[1].get("name") != "同类平均":
        raise ValueError("Source-defined peer curve identity missing")
    series = []
    for curve in curves[:2]:
        values = {}
        for point in curve["data"]:
            when = datetime.fromtimestamp(point[0]/1000, ZoneInfo("Asia/Shanghai")).date().isoformat()
            value = numeric(point[1])
            if when in values or value is None or value <= -100:
                raise ValueError("Invalid or repeated normalized curve point")
            values[when] = 1+value/100
        series.append(values)
    common = sorted(set(series[0]) & set(series[1]))
    if not common or max(series[0]) != common[-1] or max(series[1]) != common[-1]:
        raise ValueError("Peer and fund curve endpoints differ")
    end = date.fromisoformat(common[-1])
    result = {"fundCode": fund_code, "date": end.isoformat()}
    windows = {"1w": end-timedelta(days=7), "Ytd": date(end.year, 1, 1),
               **{suffix: _shift_months(end, months) for months, suffix in
                  ((1, "1m"), (3, "3m"), (6, "6m"), (12, "1y"), (36, "3y"), (60, "5y"))}}
    for suffix, beginning in windows.items():
        cutoff = beginning.isoformat()
        prior = [when for when in common if when <= cutoff]
        if not prior or (date.fromisoformat(cutoff)-date.fromisoformat(prior[-1])).days > 7:
            continue
        for index, label in ((0, ""), (1, "PeerAvg")):
            result["return"+suffix+label] = (series[index][common[-1]]/series[index][prior[-1]]-1)*100
    if len(result) == 2:
        raise ValueError("No completely covered calendar return interval")
    return [_record(fund_code, result)]


def company_rows(master, company):
    """Actual FK-backed company identity and establishment date, no invented score."""
    if (not master or not company or master.get("company_id") != company.get("company_id")
            or master.get("management") != company.get("name") or company.get("source") != "tushare"):
        raise ValueError("Company identity must match the canonical fund FK")
    if not company.get("setup_date"):
        raise ValueError("Company establishment source missing")
    return [_record(master["fund_code"], {"fundCode": master["fund_code"], "fundName": master.get("name"),
                    "companyName": company.get("short_name") or company["name"], "companyNameFull": company["name"],
                    "establishDate": day(company["setup_date"])})]


def quarter_nav_rows(raw, fund_code, ts_code):
    """Exact calendar-quarter-end NAV subset, not invented quarterly profit reports."""
    result = []
    for row in raw:
        if row.get("ts_code") != ts_code:
            raise ValueError("Quarter-end NAV fund role mismatch")
        period = day(row["nav_date"])
        suffix = period[5:]
        if suffix not in {"03-31", "06-30", "09-30", "12-31"}:
            continue
        value = numeric(row.get("unit_nav"))
        if value is None or value <= 0:
            continue
        values = {"fundCode": fund_code, "reportPeriodEnd": period, "reportType": "Q"+str(
            (3, 6, 9, 12).index(int(period[5:7]))+1), "navPerShareEnd": value}
        result.append(_record(fund_code+":"+period, values))
    if not result:
        raise ValueError("No actual exact quarter-end NAV observation")
    return result


def _get(session, endpoint, **kwargs):
    response = session.get(endpoint, timeout=(5, 20), allow_redirects=False, **kwargs)
    response.raise_for_status()
    if response.status_code != 200 or len(response.content) > 8000000:
        raise ValueError("Unexpected bounded provider response")
    return response


def _public_collect(session, kind, code, end):  # pylint: disable=too-many-return-statements
    """Fixed URLs only. No endpoint, SQL ID or source mapping comes from HTTP."""
    source = "eastmoney:disclosed-fund"
    uri = "https://fundf10.eastmoney.com/"
    if kind == "fee-tiers":
        from .product_fees import fee_rows  # pylint: disable=import-outside-toplevel
        raw = _get(session, uri+"jjfl_"+code+".html").content.decode("utf-8")
        return 243, fee_rows(raw, code), raw, "em-disclosed-original-fee-tiers-v1", source, uri
    if kind == "purchase-status":
        raw = _get(session, "https://fund.eastmoney.com/Data/Fund_JJJZ_Data.aspx",
                   params={"t": "8", "page": "1,50000", "js": "reData", "sort": "fcode,asc"}).content.decode("utf-8")
        return 238, purchase_rows(raw, code), raw, "em-current-purchase-status-cny-v1", source, uri
    if kind == "holder-ratios":
        response = _get(session, uri+"FundArchivesDatas.aspx", params={"type": "cyrjg", "code": code})
        raw = response.content.decode("utf-8")
        return 265, holder_rows(raw, code), raw, "em-holder-ratio-current-no-date-guess-v1", source, uri
    if kind == "investment-scope":
        raw = _get(session, uri+"jbgk_"+code+".html").content.decode("utf-8")
        return 241, investment_rows(raw, code), raw, "em-current-investment-scope-verbatim-v1", source, uri
    if kind == "peer-returns":
        raw = _get(session, "https://fund.eastmoney.com/pingzhongdata/"+code+".js").content.decode("utf-8")
        return 248, peer_rows(raw, code), raw, "em-source-defined-peer-curve-calendar-returns-v1", source, uri
    if kind == "industry-exposure":
        raw = _get(session, "https://api.fund.eastmoney.com/f10/HYPZ/",
                   params={"fundCode": code, "year": end[:4]}).json()
        try:
            rows = industry_rows(raw, code)
        except ValueError:
            # No rows alone cannot prove no exposure. Require a second actual
            # report whose identity, NAV and exactly-zero stock ratio verify it.
            master = _master(code)
            report = _get(session, uri+"zcpz_"+code+".html").content.decode("utf-8")
            from .product_reports import latest_report  # pylint: disable=import-outside-toplevel
            issuer = latest_report(code)
            rows = verified_zero_industry(raw, report, code, master, issuer)
            raw = {"industry": raw, "zero_stock_disclosure": report, "issuer_pdf_sha256": issuer["hash"]}
        return 264, rows, raw, "em-native-industry-nav-current-v1", source, uri
    if kind == "pcf-header":
        uri = "https://query.sse.com.cn/commonQuery.do"
        raw = _get(session, uri, headers={"Referer": "https://www.sse.com.cn/"}, params={
            "sqlId": "COMMON_SSE_CP_JJLB_ETFJJGK_GGSGSHQD_JBXX_C", "FUNDID2": code,
            "isPagination": "false"}).json()
        return 270, pcf_rows(raw, code), raw, "sse-pcf-header-prev-nav-current-estimate-v1", "sse:pcf", uri
    raise ValueError("Unreviewed public collection kind")


def ingest_collection(args):
    """Execute one offline collection; publish an atomic, evidence-backed batch."""
    import requests  # pylint: disable=import-outside-toplevel,import-error
    start, end = day(args.start), day(args.end)
    if args.kind == "manager-directory":
        return _collect_directory(args)
    if args.kind == "industry-reverse":
        return _industry_reverse(args)
    if args.kind == "manager-intervals":
        return _collect_manager(args)
    master = _master(args.fund_code)
    code = args.fund_code
    if args.kind == 'industry-exposure':
        proof = not_yet_reportable(master, args.end)
        if proof:
            version = store.record_disclosure_state(264, code, 'official-canonical-fund-inception', proof,
                                                    ttl_seconds=min(args.ttl_seconds, 86400), status='not_applicable')
            raise NotApplicableError('fund_not_established_in_completed_reporting_period',
                                     'official-canonical-fund-inception',
                                     {**proof, 'eligibility_version': version['version_id']},
                                     observed_at=version['observed_at'], valid_until=version['valid_until'])
    if args.ts_code and args.ts_code != master["ts_code"]:
        raise ValueError("Requested and canonical fund roles differ")
    if args.kind == "pcf-header" and (master.get("market") != "E" or not master["ts_code"].endswith(".SH")):
        raise ValueError("SSE exchange-fund role required")
    if args.kind == "holding-stock-returns":
        product, records, raw, method = _collect_stock_returns(master, end)
        source, uri = "tushare:fund-portfolio-stock-close", "https://tushare.pro"
    elif args.kind == "performance":
        records, raw = _collect_performance(args, master)
        from .product_performance import METHOD  # pylint: disable=import-outside-toplevel
        product, method, source, uri = 253, METHOD, "tushare:fund-nav-index", "https://tushare.pro"
    elif args.kind == "company-profile":
        company = database.select_one("fund_company", {"company_id": "eq."+str(master["company_id"])})
        records, raw = company_rows(master, company), {"master": master, "company": company}
        product, method = 244, "canonical-company-establishment-v1"
        source, uri = "tushare:fund-company", "https://tushare.pro"
    elif args.kind == "quarter-nav":
        raw = collectors.collect_tushare("fund_nav", {"ts_code": master["ts_code"],
                                         "start_date": start.replace("-", ""), "end_date": end.replace("-", "")})
        records = quarter_nav_rows(raw, code, master["ts_code"])
        product, method = 274, "exact-quarter-end-nav-observation-v1-not-quarter-profit"
        source, uri = "tushare:fund-nav", "https://tushare.pro/document/2?doc_id=119"
    else:
        with requests.Session() as session:
            session.headers.update({"Referer": "https://fundf10.eastmoney.com/"})
            try:
                product, records, raw, method, source, uri = _public_collect(session, args.kind, code, end)
            except NotDisclosedError as exc:
                if args.kind == 'industry-exposure':
                    version = store.record_disclosure_state(264, code, exc.source, exc.evidence,
                                                            ttl_seconds=min(args.ttl_seconds, 86400))
                    exc.observed_at, exc.valid_until = version['observed_at'], version['valid_until']
                    exc.evidence = {'fund_code': code, 'eligibility_version': version['version_id'],
                                    'index_sha256': collectors.evidence(exc.evidence)}
                raise
    # Snapshot windows are observation windows, never fabricated event dates.
    # A payload-specific revision avoids resurrecting removed members by merging
    # two multirow snapshots from the same source authority.
    if product == 253:
        start = end = records[0]["data"]["endDate"]
    elif product != 274:
        start = end = date.today().isoformat()
        method += ":"+collectors.evidence(raw)[:12]
    fields = set().union(*(set(record["data"]) for record in records))
    if product == 264 and not records:
        fields = {"fundCode", "industryCode", "industryName", "holdingRatioLatest"}
    return store.ingest(product, records, scope_kind="fund", scope_key=code, window_start=start, window_end=end,
                        source_name=source, method_version=method, evidence_uri=uri,
                        evidence_sha256=collectors.evidence(raw), complete_window=True, available_fields=fields,
                        ttl_seconds=min(args.ttl_seconds, 300 if product == 238 else 86400))


def _collect_performance(args, master):
    from .product_performance import performance_rows  # pylint: disable=import-outside-toplevel
    if not args.benchmark_ts_code or not args.benchmark_code:
        raise ValueError("An explicit verified benchmark role is required")
    if args.benchmark_ts_code.split(".")[0] != args.benchmark_code:
        raise ValueError("Benchmark code/role identity mismatch")
    end = day(args.end)
    start = day(args.start)
    if start == "0001-01-01":
        start = str(int(end[:4])-6)+"-01-01"
    params = {"start_date": start.replace("-", ""), "end_date": end.replace("-", "")}
    nav = collectors.collect_tushare("fund_nav", {**params, "ts_code": master["ts_code"]})
    quote = collectors.collect_tushare("index_daily", {**params, "ts_code": args.benchmark_ts_code})
    if any(row.get("ts_code") != master["ts_code"] for row in nav):
        raise ValueError("NAV identity mismatch")
    if any(row.get("ts_code") != args.benchmark_ts_code for row in quote):
        raise ValueError("Benchmark identity mismatch")
    navs = [{"date": day(row["nav_date"]), "value": collectors.number(row["adj_nav"])} for row in nav]
    quotes = [{"date": day(row["trade_date"]), "value": collectors.number(row["close"])} for row in quote]
    return performance_rows(navs, quotes, master["fund_code"]), {"nav": nav, "benchmark": quote,
                                                            "benchmark_role": args.benchmark_ts_code}


def verified_zero_industry(payload, report, fund_code, master, issuer_report=None):
    """A source-empty industry page needs independently disclosed zero stocks.

    A rounded 0.00% allocation is insufficient. Require an explicit issuer
    statement of no stocks, tied to the same period as the allocation report.
    """
    body = payload.get("Data", {})
    if (payload.get("ErrCode") != 0 or body.get("FundCode") != fund_code
            or master.get("fund_code") != fund_code or master.get("source") != "tushare"):
        raise ValueError("No verified zero-exposure source identity and eligible role")
    quarters = body.get("QuarterInfos")
    if not isinstance(quarters, list) or any(row.get("HYPZInfo") for row in quarters):
        raise ValueError("Nonempty industry records cannot become a zero-exposure attestation")
    rows = eastmoney.asset_rows(report, fund_code)
    if (len(rows) != 1 or rows[0]["data"].get("stockRatio") != 0
            or not rows[0]["data"].get("nav", 0) > 0):
        raise ValueError("A genuine positive-NAV, zero-stock disclosure is required")
    if (not issuer_report or rows[0]["source_report_period"] != issuer_report["period"]
            or not re.search(r"本基金(?:本)?报告期末(?:未持有|无)股票", re.sub(r"\s+", "", issuer_report["text"]))):
        raise ValueError("Same-period issuer statement of no stocks is required; rounded zero is not proof")
    return []


def manager_rows(document, payload, manager_id, manager_name):
    """Equal-weight interval return of every current source-listed fund.

    This is an explicitly independent current-cohort basket, not a claimed
    personal track record. No fund may be dropped when an interval is missing.
    """
    title = re.search(r"<title>(.*?)</title>", document, re.S)
    if (not title or title[1].split(" _ ")[0] != manager_name
            or not re.search(r"jlid=[\"']"+re.escape(manager_id)+r"[\"']", document)):
        raise ValueError("Manager page unique identity mismatch")
    marker = re.search(r"\bvar\s+mgrlists\s*=\s*\[(.*?)\]\s*;", document, re.S)
    if not marker:
        raise ValueError("Explicit current-manager cohort missing")
    remaining, codes = marker[1], set()
    pattern = r"\s*\{valueField:'([0-9]{6})',name:'[^'\\]*',type:'[0-9]+'\}\s*"
    while remaining:
        item = re.match(pattern, remaining)
        if not item or item[1] in codes:
            raise ValueError("Current manager cohort format changed")
        codes.add(item[1])
        remaining = remaining[item.end():]
        if remaining:
            if not remaining.startswith(","):
                raise ValueError("Unsupported manager cohort literal")
            remaining = remaining[1:]
    if not 1 <= len(codes) <= 100 or payload.get("ErrCode") != 0:
        raise ValueError("Bounded manager curve source failed")
    raw = json.loads(payload["Data"])
    if {row["valueField"] for row in raw["graph"]} != codes:
        raise ValueError("Current manager funds and curve population differ")
    points = {}
    for row in raw["dataProvider"]:
        when = day(row["date"].replace("/", "-"))
        if when in points:
            raise ValueError("Duplicate manager curve point")
        points[when] = {code: numeric(row.get(code)) for code in codes}
    end = date.fromisoformat(max(points))
    result = {"fundManagerName": manager_name}
    windows = {"returnMtd": date(end.year, end.month, 1), "returnYtd": date(end.year, 1, 1),
               **{field: _shift_months(end, months) for months, field in
                  ((1, "return1m"), (3, "return3m"), (6, "return6m"), (12, "return1y"),
                   (36, "return3y"), (60, "return5y"))}}
    for field, beginning in windows.items():
        start = beginning.isoformat()
        prior = [when for when in points if when <= start]
        if not prior or (date.fromisoformat(start)-date.fromisoformat(max(prior))).days > 7:
            continue
        start_values, end_values = points[max(prior)], points[end.isoformat()]
        if any(start_values[code] is None or end_values[code] is None or start_values[code] <= -100
               or end_values[code] <= -100 for code in codes):
            continue
        result[field] = sum(((1+end_values[code]/100)/(1+start_values[code]/100)-1)*100
                            for code in codes)/len(codes)
    if len(result) == 1:
        raise ValueError("No interval covers the complete current-manager population")
    return [_record(manager_id, result)]


def _collect_manager(args):
    import requests  # pylint: disable=import-outside-toplevel,import-error
    if not args.manager_id or not re.fullmatch(r"[0-9]{6,12}", args.manager_id):
        raise ValueError("An explicit public-source manager identity is required")
    if not args.manager_name or not args.manager_name.strip() or len(args.manager_name) > 128:
        raise ValueError("An exact manager name is required")
    if store.manager_identity(args.manager_name) != args.manager_id:
        raise ValueError("Name is ambiguous or does not match the complete current-manager directory")
    uri = "https://fund.eastmoney.com/manager/"+args.manager_id+".html"
    with requests.Session() as session:
        session.headers.update({"Referer": uri})
        document = _get(session, uri).content.decode("utf-8-sig")
        payload = _get(session, "https://api.fund.eastmoney.com/Manager/SYLPM",
                       params={"mrgid": args.manager_id}).json()
    records = manager_rows(document, payload, args.manager_id, args.manager_name)
    method = "em-current-fund-cohort-equalweight-"+args.manager_id+"-v1"
    with database.get_db().transaction(read_only=True) as conn:
        # pylint: disable=protected-access
        existing = store._sql(conn, "SELECT DISTINCT source_name FROM open_fund_product_batch WHERE product_id=251 "
                              "AND scope_kind='manager' AND scope_key=?", (args.manager_name,), fetch=True)
        # pylint: enable=protected-access
    source = "eastmoney:manager:"+args.manager_id
    if any(row["source_name"] != source for row in existing):
        raise ValueError("Represented same-name managers cannot be merged by name")
    today = date.today().isoformat()
    return store.ingest(251, records, scope_kind="manager", scope_key=args.manager_name,
                        window_start=today, window_end=today, source_name=source, method_version=method,
                        evidence_uri=uri, evidence_sha256=collectors.evidence({"profile": document, "curve": payload}),
                        complete_window=True, ttl_seconds=min(args.ttl_seconds, 86400))


def _industry_reverse(args):
    """Only publish after every active governed catalog fund has a verified batch."""
    # pylint: disable=protected-access
    with database.get_db().transaction(read_only=True) as conn:
        masters = store._sql(conn, "SELECT fund_code FROM fund_basic_ext WHERE status='L' AND source='tushare' "
                             "ORDER BY fund_code LIMIT 100001", fetch=True)
    if not masters or len(masters) > 100000:
        raise ValueError("A bounded complete active canonical fund catalog is required")
    records, evidence, names = [], [], {}
    eligible, undisclosed, not_applicable = 0, 0, 0
    expiry = datetime.now().timestamp()+min(args.ttl_seconds, 86400)
    for master in masters:
        code = master["fund_code"]
        with database.get_db().transaction(read_only=True) as conn:
            jobs = store._sql(conn, "SELECT recorded_at FROM open_fund_product_batch WHERE product_id=264 "
                              "AND scope_kind='fund' AND scope_key=? AND status='ready' "
                              "ORDER BY recorded_at DESC,batch_id DESC LIMIT 1", (code,), fetch=True)
            proof = store.disclosure_state(conn, 264, code, after=jobs[0]['recorded_at'] if jobs else 0)
        if proof:
            undisclosed += int(proof['status'] == 'not_disclosed')
            not_applicable += int(proof['status'] == 'not_applicable')
            expiry = min(expiry, proof['valid_until'])
            evidence.append({'fundCode': code, 'eligibility': proof['status'],
                             'source_version': proof['version_id'], 'sha256': proof['evidence_sha256']})
            continue
        eligible += 1
        body = store.query_product(264, {"fundCode": code, "pageNum": 1, "pageSize": 500})
        expiry = min(expiry, body["valid_until"])
        if body["total"] != len(body["items"]):
            raise ValueError("A catalog member's industry snapshot was truncated")
        if not all(method.startswith("em-native-industry-nav-current-v1:") for method in body["method_version"]):
            raise ValueError("Industry reverse cannot mix unknown classification methods")
        evidence.append({"fundCode": code, "total": body["total"], "versions": body["method_version"]})
        for row in body["items"]:
            key = row["industryCode"]
            if key in names and names[key] != row["industryName"]:
                raise ValueError("Industry codes have conflicting source classification meanings")
            names[key] = row["industryName"]
            records.append(_record(code+":"+key, row))
    if len(records) > 100000 or args.product not in {262, 268}:
        raise ValueError("Supported reverse products are 262/268 with bounded complete rows")
    today = date.today().isoformat()
    digest = collectors.evidence(evidence)
    if not eligible:
        raise NotDisclosedError('no_catalog_fund_disclosed_industry_yet', 'complete-catalog-disclosure-eligibility',
                                {'catalog_fund_count': len(masters), 'eligible_fund_count': 0,
                                 'undisclosed_fund_count': undisclosed, 'not_applicable_fund_count': not_applicable,
                                 'sha256': digest}, valid_until=expiry)
    remaining = int(expiry-datetime.now().timestamp())
    if remaining <= 0:
        raise LookupError('Industry source eligibility expired while materializing')
    return store.ingest(args.product, records, scope_kind="global", scope_key="*", window_start=today,
                        window_end=today, source_name="ifund:complete-active-catalog-disclosed-industry",
                        method_version="verified-disclosed-catalog-industry-nav-reverse-v2:"+digest[:12],
                        evidence_uri="https://api.fund.eastmoney.com/f10/HYPZ/", evidence_sha256=digest,
                        complete_window=True, available_fields={"fundCode", "industryCode", "industryName",
                                                               "holdingRatioLatest"},
                        ttl_seconds=min(args.ttl_seconds, remaining),
                        scope_metadata={'catalog_fund_count': len(masters), 'eligible_fund_count': eligible,
                                        'undisclosed_fund_count': undisclosed,
                                        'not_applicable_fund_count': not_applicable})


def stock_return_rows(portfolio, quotes, fund_code, ts_code, asof, capitalizations=None):
    """Latest disclosed stock holdings, exact role, unadjusted calendar-YTD price return."""
    reports = []
    for row in portfolio:
        if row.get("ts_code") != ts_code:
            raise ValueError("Portfolio fund role mismatch")
        if row.get("ann_date") and day(row["ann_date"]) <= asof and day(row["end_date"]) <= asof:
            reports.append(row)
    if not reports:
        raise ValueError("No actually disclosed portfolio")
    period = max(day(row["end_date"]) for row in reports)
    current = [row for row in reports if day(row["end_date"]) == period]
    if not 1 <= len(current) <= 100 or len({row["symbol"] for row in current}) != len(current):
        raise ValueError("Portfolio stock population exceeds bound or has revisions")
    result = []
    for holding in current:
        symbol = holding["symbol"]
        prices = {}
        for row in quotes[symbol]:
            if row.get("ts_code") != symbol:
                raise ValueError("Stock quote canonical identity mismatch")
            when, value = day(row["trade_date"]), numeric(row.get("close"))
            if when <= asof and value is not None and value > 0:
                if when in prices and prices[when] != value:
                    raise ValueError("Conflicting stock close prices")
                prices[when] = value
        cutoff = asof[:4]+"-01-01"
        before = [when for when in prices if when <= cutoff]
        if not prices:
            raise ValueError("Missing genuine current stock close")
        end = max(prices)
        if (date.fromisoformat(asof)-date.fromisoformat(end)).days > 7:
            raise ValueError("Current stock quote is stale")
        values = {"fundCode": fund_code, "stockCode": symbol.split(".")[0], "endDate": end}
        if before and (date.fromisoformat(cutoff)-date.fromisoformat(max(before))).days <= 10 and max(before) != end:
            values["returnYtd"] = (prices[end]/prices[max(before)]-1)*100
        cap = (capitalizations or {}).get(symbol)
        if cap:
            if cap.get("ts_code") != symbol or day(cap["trade_date"]) != end:
                raise ValueError("Market-cap identity or date differs from the close")
            value = numeric(cap.get("total_mv"))
            if value is not None and value > 0:
                values["marketCap"] = value*10000  # official total_mv unit: 10k CNY
        if "returnYtd" not in values and "marketCap" not in values:
            raise ValueError("Neither a genuine YTD start nor same-date market capitalization is available")
        result.append(_record(fund_code+":"+symbol, values))
    return result


def _collect_stock_returns(master, asof):
    """Fixed role-specific providers, bounded to 100 actually disclosed stocks."""
    year = int(asof[:4])
    portfolio = collectors.collect_tushare("fund_portfolio", {"ts_code": master["ts_code"],
                                              "start_date": f"{year-1}0101", "end_date": asof.replace("-", "")})
    reports = [row for row in portfolio if row.get("ann_date") and day(row["ann_date"]) <= asof]
    if not reports:
        raise NotDisclosedError("no_stock_portfolio_disclosed_in_window", "official-tushare-fund-portfolio",
                                {"ts_code": master["ts_code"], "observed_rows": len(portfolio),
                                 "as_of": asof, "sha256": collectors.evidence(portfolio)})
    period = max(day(row["end_date"]) for row in reports)
    symbols = sorted({row["symbol"] for row in reports if day(row["end_date"]) == period})
    if not 1 <= len(symbols) <= 100:
        raise ValueError("Stock population exceeds offline bound")
    quotes, capitalizations = {}, {}
    for symbol in symbols:
        if re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", symbol):
            api = "daily"
        elif re.fullmatch(r"[0-9]{5}\.HK", symbol):
            api = "hk_daily"
        else:
            raise ValueError("No reviewed canonical quote role for this portfolio symbol")
        quotes[symbol] = collectors.collect_tushare(api, {"ts_code": symbol,
                                                    "start_date": f"{year-1}1215",
                                                    "end_date": asof.replace("-", "")},
                                                  fields="ts_code,trade_date,close")
        if api == "daily" and not any(day(row["trade_date"]) <= asof[:4]+"-01-01" for row in quotes[symbol]):
            latest = max(day(row["trade_date"]) for row in quotes[symbol])
            raw = collectors.collect_tushare("daily_basic", {"ts_code": symbol,
                                             "trade_date": latest.replace("-", "")},
                                             fields="ts_code,trade_date,total_mv")
            if len(raw) != 1:
                raise ValueError("Market-cap source must verify one exact stock/day")
            capitalizations[symbol] = raw[0]
    records = stock_return_rows(portfolio, quotes, master["fund_code"], master["ts_code"], asof, capitalizations)
    return 269, records, {"portfolio": portfolio, "quotes": quotes, "capitalizations": capitalizations}, (
        "disclosed-stock-unadjusted-price-ytd-v1")



def directory_rows(pages):
    """All declared pages/records and unique IDs are required, not one search result."""
    decoded = [assignment(document, "managerDirectory") for document in pages]
    if not decoded or not 1 <= len(decoded) <= 500:
        raise ValueError("Bounded current-manager directory pages required")
    declared, page_count = decoded[0]["record"], decoded[0]["pages"]
    counts_are_integers = all(isinstance(value, int) and not isinstance(value, bool)
                              for value in [declared, page_count, *(row["curpage"] for row in decoded)])
    if (not counts_are_integers or not 1 <= declared <= 20000
            or page_count != len(decoded) or {row["curpage"] for row in decoded} != set(range(1, page_count+1))):
        raise ValueError("Manager directory pages are incomplete")
    rows = []
    for page in decoded:
        if page["record"] != declared or page["pages"] != page_count or not isinstance(page["data"], list):
            raise ValueError("Manager directory totals changed during collection")
        for raw in page["data"]:
            if not isinstance(raw, list) or len(raw) != 12:
                raise ValueError("Manager directory row schema changed")
            rows.append({"id": raw[0], "name": raw[1]})
    if (len(rows) != declared or len({row["id"] for row in rows}) != declared
            or [row["id"] for row in rows] != sorted((row["id"] for row in rows), key=int)):
        raise ValueError("Manager directory is truncated, duplicated or not strictly ID-sorted")
    return rows


def _collect_directory(args):
    import requests  # pylint: disable=import-outside-toplevel,import-error
    from concurrent.futures import ThreadPoolExecutor  # pylint: disable=import-outside-toplevel
    endpoint = "https://fund.eastmoney.com/data/FundDataPortfolio_Interface.aspx"
    def fetch(page):
        with requests.Session() as session:
            return _get(session, endpoint, params={"dt": 14, "ft": "all", "mc": "managerDirectory",
                        "sc": "MGRID", "st": "asc", "pn": 50, "pi": page}).content.decode("utf-8")
    first = fetch(1)
    count = assignment(first, "managerDirectory")["pages"]
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 500:
        raise ValueError("Current-manager directory page bound changed")
    with ThreadPoolExecutor(max_workers=4) as executor:
        pages = [first, *executor.map(fetch, range(2, count+1))]
    rows = directory_rows(pages)
    return store.publish_manager_directory(rows, len(rows), collectors.evidence(pages), args.ttl_seconds)
