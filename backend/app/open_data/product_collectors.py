"""Offline source adapters. Never import or invoke these from a Flask route."""
from __future__ import annotations

import hashlib
import json
import math
import os
from urllib.parse import urlsplit

from .product_contracts import contracts, day


def number(value):
    """Normalize a documented numeric source field; booleans are never amounts."""
    if value is None or value in ("", "-", "--", "—"):
        return None
    if isinstance(value, bool):
        raise ValueError("Boolean amount")
    result = float(value)
    if math.isnan(result):
        return None
    if not math.isfinite(result):
        raise ValueError("Nonfinite source amount")
    return result


def _record(key, values, **metadata):
    return {"record_key": str(key), "data": {name: value for name, value in values.items() if value is not None},
            **metadata}


def money_rows(raw, fund_code):
    """East Money's money-fund income is NOT unit or accumulated NAV."""
    result = []
    for row in raw:
        when = day(row["净值日期"])
        result.append(_record(fund_code+":"+when,
                              {"fundCode": fund_code, "date": when,
                               "avgDailyProfitPer10k": number(row.get("每万份收益")),
                               "avg7dYieldAnn": number(row.get("7日年化收益率"))}))
    return result


def announcement_rows(raw, fund_code):
    """Do not recast East Money's AN... identity into the vendor's int32 ID."""
    result = []
    for row in raw:
        if str(row.get("基金代码", fund_code)) != fund_code or not row.get("报告ID"):
            raise ValueError("Announcement identity not verified")
        result.append(_record(row["报告ID"], {"fundCode": fund_code, "fundName": row.get("基金名称"),
                                             "date": day(row["公告日期"]), "title": row["公告标题"],
                                             "announcementSource": "东方财富基金公告"}))
    return result


def manager_rows(raw, fund_code, ts_code):
    """Tushare biography and exact dates; birth year is never a fake birthday."""
    result = []
    for row in raw:
        if row.get("ts_code") != ts_code:
            raise ValueError("Manager fund role mismatch")
        start, end = row.get("begin_date"), row.get("end_date")
        values = {"fundCode": fund_code, "fundManagerName": row["name"],
                  "announceDate": day(row["ann_date"]) if row.get("ann_date") else None,
                  "startDate": day(start) if start else None, "endDate": day(end) if end else None,
                  "gender": {"M": "男", "F": "女"}.get(row.get("gender")),
                  "educationLevel": row.get("edu"), "backgroundDesc": row.get("resume")}
        # An explicit resignation is evidence of a former manager. A null
        # resignation date alone is not evidence of continuing employment.
        if end:
            values["isIncumbent"] = 0
        result.append(_record("|".join((ts_code, row["name"], str(start), str(end))), values))
    return result


def margin_rows(raw, fund_code, ts_code):
    """Tushare margin-detail amounts are yuan, quantities are shares."""
    mapping = {"rzye": "marginBalance", "rzmre": "marginBuyAmount", "rzche": "marginRepayAmount",
               "rqyl": "shortBalanceVolume", "rqmcl": "shortSellVolume", "rqye": "shortBalanceAmount",
               "rzrqye": "marginShortBalance"}
    result = []
    for row in raw:
        if row.get("ts_code") != ts_code:
            raise ValueError("Margin fund role mismatch")
        when = day(row["trade_date"])
        values = {target: number(row.get(source)) for source, target in mapping.items()}
        result.append(_record(ts_code+":"+when, {"fundCode": fund_code, "date": when, **values},
                              is_etf=True))
    return result


def pcf_component_rows(raw, fund_code, ts_code, publish_dates=None):
    """A PCF effective day is not silently relabelled as its publication date.

    publish_dates must come from separately parsed issuer/exchange PCF headers.
    The reference's premium/discount fields are integer; nonintegral source
    percentages are omitted, never truncated or repurposed as a cash ratio.
    """
    result = []
    for row in raw:
        if row.get("ts_code") != ts_code:
            raise ValueError("PCF fund role mismatch")
        effective = day(row["trade_date"])
        published = (publish_dates or {}).get(effective)
        flag = row.get("sub_flag")
        values = {"fundCode": fund_code, "publishDate": day(published) if published else None,
                  "stockCode": str(row["con_code"]).split(".", maxsplit=1)[0], "stockName": row.get("con_name"),
                  "stockQuantity": number(row.get("qty")), "cashSubstituteFlagDesc": flag,
                  "cashSubstituteFlag": {"允许": 1, "必须": 2, "禁止": 3, "退补": 4}.get(flag),
                  "subscribeSubstituteAmount": number(row.get("sub_cc")),
                  "redeemSubstituteAmount": number(row.get("red_cc"))}
        if flag == "必须" and row.get("sca") is not None:
            values["fixedSubstituteAmount"] = number(row["sca"])
        for source, target in (("cpr", "subscribeCashSubstitutePremiumRatio"),
                               ("rdr", "redeemCashSubstituteDiscountRatio")):
            value = number(row.get(source))
            if value is not None and value.is_integer():
                values[target] = int(value)
        result.append(_record(ts_code+":"+effective+":"+str(row["con_code"]), values, is_etf=True))
    return result


def collect_tushare(api_name, params, fields="", *, page_size=3000, max_pages=100):
    """Use the project's shared limiter and credentials, with truncation guards."""
    from app.fund_nav.fetch import tushare_client  # pylint: disable=import-outside-toplevel
    # This collector is invoked only in an offline process. Upgrade the
    # legacy client's historical plaintext default; never send a key to a
    # configurable proxy or an unreviewed host from the open-data pipeline.
    if tushare_client.ENDPOINT == "http://api.tushare.pro":
        tushare_client.ENDPOINT = "https://api.tushare.pro"
    if tushare_client.ENDPOINT != "https://api.tushare.pro":
        raise ValueError("Open-data collection requires the official HTTPS source")
    reviewed = {"fund_manager", "margin_detail", "etf_sh_cons", "etf_sz_cons", "index_daily",
                "fund_daily", "fund_portfolio", "fund_nav", "daily", "hk_daily", "daily_basic",
                "fund_basic", "fund_company", "fund_div", "fund_share"}
    if api_name not in reviewed:
        raise ValueError("Unreviewed Tushare operation")
    result, seen, byte_count = [], set(), 0
    for index in range(max_pages):
        batch = tushare_client.call(api_name, {**params, "limit": page_size, "offset": index*page_size}, fields)
        if not isinstance(batch, list):
            raise ValueError("Invalid provider list")
        serialized = json.dumps(batch, sort_keys=True, default=str).encode()
        byte_count += len(serialized)
        if byte_count > 32000000 or len(result)+len(batch) > 100000:
            raise ValueError("Provider observation budget exceeded")
        fingerprint = hashlib.sha256(serialized).hexdigest()
        if batch and fingerprint in seen:
            raise ValueError("Provider ignored pagination; window is not complete")
        seen.add(fingerprint)
        result.extend(batch)
        if len(batch) < page_size:
            return result
    raise ValueError("Provider pagination bound reached; window is not complete")


def collect_akshare(kind, fund_code):
    """CLI/subprocess entry only, never callable through the read service."""
    import akshare as ak  # pylint: disable=import-outside-toplevel,import-error
    if kind == "money-income":
        frame = ak.fund_money_fund_info_em(symbol=fund_code)
        expected = {"净值日期", "每万份收益", "7日年化收益率"}
        if not expected <= set(frame.columns):
            raise ValueError("Money-income source schema changed")
        return frame.to_dict("records")
    if kind == "announcements":
        rows = []
        for name in ("fund_announcement_report_em", "fund_announcement_dividend_em", "fund_announcement_personnel_em"):
            frame = getattr(ak, name)(symbol=fund_code)
            if not {"基金代码", "公告日期", "公告标题", "报告ID"} <= set(frame.columns):
                raise ValueError("Announcement source schema changed")
            rows.extend(frame.to_dict("records"))
        unique = {str(row["报告ID"]): row for row in rows}
        return list(unique.values())
    raise ValueError("Unreviewed AKShare collector")


def _path(payload, path):
    for key in path.split(".") if path else []:
        payload = payload[int(key)] if isinstance(payload, list) else payload[key]
    return payload


def configured_provider(manifest, context):
    """Bounded HTTPS ingestion for reviewed issuer/exchange/licensed mappings.

    No URL, host, field mapping or credential is supplied by a public request.
    Operators keep manifests outside the web root and fixture-test them first.
    This supports all frozen fund fields without adding a production proxy to
    the comparison vendor. Header secrets stay in environment variables.
    """
    import requests  # pylint: disable=import-outside-toplevel,import-error
    endpoint = manifest["endpoint"]
    target = urlsplit(endpoint)
    allowed = manifest.get("approved_hosts", [])
    unsafe = any((target.username, target.password, target.query, target.fragment,
                  target.port not in (None, 443)))
    comparison = any(part in str(target.hostname).lower() for part in ("invetbot", "investoday"))
    if target.scheme != "https" or target.hostname not in allowed or unsafe or comparison:
        raise ValueError("Provider endpoint is not an approved independent HTTPS source")
    if manifest.get("method", "GET") not in {"GET", "POST"}:
        raise ValueError("Provider method must be GET or POST")
    headers = {key: os.environ[name] for key, name in manifest.get("header_env", {}).items()}
    page_size = int(manifest.get("page_size", 500))
    if not 1 <= page_size <= 5000:
        raise ValueError("Invalid provider page size")
    output, seen, byte_count = [], set(), 0
    for index in range(100):
        params = {key: context[value[1:]] if isinstance(value, str) and value.startswith("$") else value
                  for key, value in manifest.get("parameters", {}).items()}
        params[manifest.get("limit_parameter", "limit")] = page_size
        params[manifest.get("offset_parameter", "offset")] = index*page_size
        method = manifest.get("method", "GET")
        with requests.request(method, endpoint, params=params if method == "GET" else None,
                              json=params if method == "POST" else None, headers=headers, timeout=(5, 30),
                              allow_redirects=False, stream=True) as response:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_content(65536):
                body.extend(chunk)
                if len(body) > 16000000:
                    raise ValueError("Provider response exceeds bound")
            payload = json.loads(body)
        if manifest.get("success_path") is not None:
            if _path(payload, manifest["success_path"]) != manifest["success_value"]:
                raise ValueError("Provider reported an unsuccessful business response")
        rows = _path(payload, manifest.get("rows_path", ""))
        if not isinstance(rows, list):
            raise ValueError("Provider rows path changed")
        serialized = json.dumps(rows, sort_keys=True).encode()
        byte_count += len(serialized)
        if byte_count > 32000000 or len(output)+len(rows) > 100000:
            raise ValueError("Provider observation budget exceeded")
        fingerprint = hashlib.sha256(serialized).hexdigest()
        if rows and fingerprint in seen:
            raise ValueError("Provider repeated a page")
        seen.add(fingerprint)
        output.extend(rows)
        if len(rows) < page_size:
            return output
    raise ValueError("Provider pagination was not exhausted")


def mapped_rows(product, raw, manifest, context):
    """Audited field/unit mapping, no eval, SQL, guessing or hidden FX conversion."""
    data = contracts()[product]["responseSchema"]["properties"]["data"]
    fields = data.get("items", data).get("properties", {})
    mapping = manifest["field_map"]
    if set(mapping)-set(fields) or not manifest.get("identity_paths"):
        raise ValueError("Unreviewed fields or natural identity")
    result = []
    for item in raw:
        row = {}
        for target, definition in mapping.items():
            if "context" in definition:
                value = context[definition["context"]]
            else:
                value = _path(item, definition["path"])
            if value is None:
                continue
            if "enum_map" in definition:
                value = definition["enum_map"][str(value)]
            kind = fields[target].get("type")
            if kind == "integer" and fields[target].get("enum") and all(
                    isinstance(value, str) for value in fields[target]["enum"]):
                kind = "string"
            if kind in {"number", "integer"}:
                value = number(value)
                scale = definition.get("multiplier", 1)
                if type(scale) not in (int, float) or scale not in {1, .01, .0001, 100, 1000, 10000, 100000000}:
                    raise ValueError("Financial unit conversion is not reviewed")
                value = value*scale if value is not None else None
                if kind == "integer" and value is not None:
                    if not value.is_integer():
                        raise ValueError("Cannot truncate a numeric source into an integer field")
                    value = int(value)
            if definition.get("date") and value is not None:
                value = day(value)
            row[target] = value
        identity = json.dumps([_path(item, path) for path in manifest["identity_paths"]], ensure_ascii=False)
        if all(value is None for value in json.loads(identity)):
            raise ValueError("Empty provider identity")
        result.append(_record(identity, row))
    return result


def evidence(raw):
    """Content hash only; credentials never enter the persisted source artifact."""
    return hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
