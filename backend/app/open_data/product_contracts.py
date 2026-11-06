"""Frozen fund-product contracts shared by offline ingest and fixed reads."""
from __future__ import annotations

from datetime import date
from functools import lru_cache
import json
import math
from pathlib import Path
import re

DATE_FIELDS = {223: "date", 224: "date", 225: "date", 226: "dataTime", 227: "date", 228: "date",
               229: "date", 230: "date", 231: "date", 238: "resumeDate", 240: "startDate",
               241: "publishDate", 243: "publishDate", 245: "date", 246: "date", 247: "date",
               251: "date", 252: "date", 253: "endDate", 254: "date", 255: "date", 256: "exDividendDate",
               257: "splitExDate", 265: "reportDate", 266: "endDate", 270: "publishDate",
               271: "publishDate", 272: "date", 273: "reportPeriodEnd", 274: "reportPeriodEnd"}
IDENTITY_FIELDS = frozenset({"fundCode", "fundId", "fundName", "fundManagerName", "fundManagerCode",
                             "date", "publishDate", "reportDate", "reportPeriodEnd", "endDate", "startDate",
                             "dataTime", "sysTime", "benchmarkIndexCode", "benchmarkIndexName",
                             "industryCode", "industryName", "conceptCode", "conceptName"})


REQUIRED_MEASURES = {142: {"holdingAssetAnnual", "holdingNavAnnual"}, 143: {"holdingWeightAnnual"},
    226: {"currentPrice"}, 240: {"benchmarkWeightPct"}, 247: {"avgDailyProfitPer10k", "avg7dYieldAnn"},
    251: {"returnMtd", "return1m", "return3m", "return6m", "returnYtd", "return1y", "return3y", "return5y"},
    252: {"returnTenure", "isIncumbent"}, 254: {"closePrice", "chg", "changePct"},
    255: {"corr6m", "corr1y", "corr3y", "corr5y"}, 272: {"premiumRate"},
    273: {"navChangePctCurrent", "navReturnWeightedAvg", "distributableIncome", "distributableIncomePerShare",
          "navPerShareEnd", "navReturnCurrent", "profitExclFairValueChange", "navEnd", "navReturnCumulative",
          "profitCurrent", "fairValueChangeIncome", "profitPerShareWeightedAvg", "profitMarginPerShareWeightedAvg",
          "navAccAdjEnd", "totalNavEnd"},
}
REQUIRED_MEASURES[274] = REQUIRED_MEASURES[273]
REQUIRED_MEASURES.update({
    238: {"subscribeLimit", "remark"}, 241: {"description"}, 244: {"establishDate"},
    248: {"return1m", "return3m", "return6m", "return1mPeerAvg", "return3mPeerAvg", "return6mPeerAvg"},
    262: {"holdingRatioLatest"}, 264: {"holdingRatioLatest"}, 268: {"holdingRatioLatest"},
    265: {"instHoldingRatio", "retailHoldingRatio"},
    269: {"returnYtd", "marketCap"},
    270: {"nav", "navPerCreationUnit", "cashBalance", "estimatedCashComponent", "creationUnit"},
})


@lru_cache(maxsize=1)
def contracts():
    """Load the checked-in contract artifact without touching DB or providers."""
    payload = json.loads(Path(__file__).with_name("fund_contracts.json").read_text(encoding="utf-8"))
    return {row["number"]: row for row in payload["operations"]}


def day(value):
    """Normalize a genuine date/timestamp to its date part, never infer a quarter."""
    text = str(value)
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return date.fromisoformat(text[:10]).isoformat()


def _validate(value, schema, path):
    if value is None:
        return
    incumbent = path.endswith(".isIncumbent") and "0代表历任" in schema.get("description", "")
    if incumbent and (not isinstance(value, int) or isinstance(value, bool) or value not in (0, 1)):
        raise ValueError(f"Invalid incumbent state at {path}")
    if "enum" in schema and value not in schema["enum"] and not incumbent:
        raise ValueError(f"Invalid enum at {path}")
    kind = schema.get("type")
    # Two frozen reportType fields declare integer but enumerate string codes.
    # Preserve the explicit codes; the contradiction remains in sourceWarnings.
    if kind == "integer" and schema.get("enum") and all(isinstance(x, str) for x in schema["enum"]):
        kind = "string"
    valid = {"string": isinstance(value, str), "integer": isinstance(value, int) and not isinstance(value, bool),
             "number": isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value),
             "boolean": isinstance(value, bool), "object": isinstance(value, dict),
             "array": isinstance(value, list)}
    if kind in valid and not valid[kind]:
        raise ValueError(f"Invalid type at {path}")
    if kind == "object":
        properties = schema.get("properties", {})
        if set(value)-set(properties):
            raise ValueError(f"Unknown field at {path}")
        for key in schema.get("required", []):
            if value.get(key) is None:
                raise ValueError(f"Missing required field at {path}.{key}")
        for key, child in value.items():
            _validate(child, properties[key], path+"."+key)
    if kind == "array":
        for child in value:
            _validate(child, schema.get("items", {}), path+"[]")


def validate_row(product, row):
    """Reject wrong units' types, unknown fields and identifier-only placeholders."""
    operation = contracts()[product]
    data = operation["responseSchema"]["properties"]["data"]
    schema = data.get("items", data) if data.get("type") == "array" else data
    _validate(row, schema, "data[]")
    if not any(value is not None and key not in IDENTITY_FIELDS for key, value in row.items()):
        raise ValueError("An identifier-only row is not a product fact")
    if product in REQUIRED_MEASURES and not any(row.get(field) is not None for field in REQUIRED_MEASURES[product]):
        raise ValueError("The product has no actual required business measure")
    if row.get("fundCode") is not None and not re.fullmatch(r"[0-9]{6}", row["fundCode"]):
        raise ValueError("Invalid fundCode")
    return row


def validate_query(product, query):
    """Validate source callers too; trusted JWT is not arbitrary query authority."""
    operation = contracts()[product]
    schema = operation["requestSchema"]
    if not schema:
        properties = {item["name"]: {"type": item["type"]} for item in operation["parameters"]}
        schema = {"type": "object", "properties": properties}
    _validate(query, schema, "query")
    if product in {142, 143}:
        other = 'industryCodes' if product == 142 else 'conceptCodes'
        if bool(query.get('fundCodes')) == bool(query.get(other)):
            raise ValueError('Provide exactly one fundCodes or association selector')
        if 'minAnnualWeight' in query and not 0 <= query['minAnnualWeight'] <= 100:
            raise ValueError('Annual weight must lie in zero to one hundred')
    for single, multiple in (("fundCode", "fundCodes"), ("benchmarkIndexCode", "benchmarkIndexCodes"),
                             ("industryCode", "industryCodes"), ("conceptCode", "conceptCodes")):
        if single not in schema.get("properties", {}) and multiple not in schema.get("properties", {}):
            continue
        left, right = query.get(single), query.get(multiple)
        if product in {142, 143} and left is None and right is None:
            continue
        if single in schema.get("properties", {}) and multiple in schema.get("properties", {}):
            if (left is None) == (right is None):
                raise ValueError(f"Provide exactly one of {single}/{multiple}")
        values = right if right is not None else [left] if left is not None else []
        if not values or len(values) > 20 or len(set(values)) != len(values):
            raise ValueError("Invalid or oversized selector")
        if any(not isinstance(value, str) or not value or len(value) > 64 for value in values):
            raise ValueError("Invalid selector value")
        if multiple == "fundCodes" and any(not re.fullmatch(r"[0-9]{6}", value) for value in values):
            raise ValueError("Invalid fund code")
    for field in ("fundManagerName", "managerName", "title"):
        if field in query and (not str(query[field]).strip() or len(query[field]) > 128):
            raise ValueError("Invalid text filter")
    for field in ("beginDate", "endDate"):
        if query.get(field) is not None and day(query[field]) != query[field]:
            raise ValueError("Date filters must be YYYY-MM-DD")
    if query.get("beginDate") and query.get("endDate") and query["beginDate"] > query["endDate"]:
        raise ValueError("Date window must be ordered")
    if not 1 <= query.get("pageNum", 1) <= 100000 or not 1 <= query.get("pageSize", 100) <= 500:
        raise ValueError("Invalid page bounds")
    return query
