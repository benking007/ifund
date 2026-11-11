"""Authenticated, bounded read models. These endpoints never fetch or write data."""
from __future__ import annotations

from datetime import date, datetime
import math
import json
import os
import re

from flask import Blueprint, jsonify, request
from flask_jwt_extended import verify_jwt_in_request

from app import db as database
from .product_routes import register_product_routes
from .product_base_read import require_base
from .product_nav_types import NAV_UNIT_TYPES

bp = Blueprint("open_data", __name__, url_prefix="/api/open-data/v1")
register_product_routes(bp)
_CODE = re.compile(r"^[0-9]{6}$")
_QUARTER = re.compile(r"^[0-9]{4}Q[1-4]$")
_COMMON = {"fund_code", "fund_codes", "start_date", "end_date", "page", "page_size"}
_MASTER = ("code", "name", "type", "fund_type")
_DETAILS = ("fund_code", "trade_date", "fund_name", "fund_full_name", "establish_date", "scale", "fund_company",
            "fund_manager", "custodian_bank", "benchmark", "position_stock", "position_bond",
            "position_cash", "position_other", "return_1m", "return_3m", "return_6m", "return_1y",
            "return_3y", "return_5y", "return_ytd", "return_since_inception", "invest_target", "invest_strategy")
_EXT = ("ts_code", "market", "status", "list_date", "delist_date", "found_date", "issue_date", "due_date",
        "fund_category", "management", "custodian", "management_fee", "custodian_fee", "sales_service_fee")
# funds.type is the source's actual classification (fund_name_em), whereas
# fund_type=stock/non_stock is merely this app's screening bucket.  In legacy
# fund_nav, money-fund Data_millionCopiesIncome is also stored in nav; it is
# income per ten thousand shares, never a verified unit or adjusted NAV.
# Keep this list explicit: a new/empty classification is not evidence of NAV
# semantics and must be reviewed before the public source serves its values.
_UNIT_NAV_TYPES = NAV_UNIT_TYPES


@bp.before_request
def authenticate():
    """Reuse the existing PAT exchange and short-lived JWT authentication."""
    verify_jwt_in_request()


@bp.errorhandler(ValueError)
def invalid_request(exc):
    """Return validation errors without database or deployment details."""
    return jsonify({"error": "invalid_request", "message": str(exc)}), 400


@bp.errorhandler(LookupError)
def unavailable_base(_exc):
    """Expose no SQL, deployment internals or fabricated successful empties."""
    return jsonify({'error': 'product_window_incomplete', 'reason': 'source_window_not_verified'}), 503


@bp.get("/_ready")
def open_data_ready():
    """Authenticated, bounded read-only schema and current-fact readiness."""
    from .product_store import _sql  # pylint: disable=import-outside-toplevel,protected-access
    try:
        with database.get_db().transaction(read_only=True) as conn:
            required = {
                'users': 'id,username', 'api_tokens': 'id,user_id,token_hash,last_used_at',
                'funds': 'code,name,type,fund_type', 'fund_basic_ext': 'fund_code,ts_code,status,source',
                'fund_nav': 'fund_code,trade_date,nav,acc_nav,adj_nav,adj_src',
                'fund_holdings': 'fund_code,quarter,holding_type,asset_code,hold_amount,hold_market_value,hold_ratio',
                'fund_manager_tenure': 'fund_code,managers,start_date,end_date,tenure_return',
                'fund_div_split': 'fund_code,event_type,ex_date',
                'open_fund_base_watermark': 'version_id,kind,scope_id,row_count,observed_at',
                'open_fund_product_batch': 'batch_id,product_id,scope_kind,scope_key,recorded_at,'
                                           'row_count,scope_metadata_json',
                'open_fund_etf_directory': 'version_id,cohort_hash,row_count,payload_hash',
                'open_fund_etf_generation': 'generation_id,product_id,cohort_hash,fact_count,scope_count',
                'open_fund_etf_generation_scope': 'generation_id,fund_code,source_version,eligibility_status,row_count',
                'open_fund_etf_generation_fact': 'generation_id,fund_code,association_code,annual_weight,payload_hash',
                'open_fund_product_fact': 'batch_id,product_id,fund_code,event_date,payload_json,etf_flag',
                'open_fund_manager_directory_batch': 'snapshot_id,declared_count,recorded_at',
                'open_fund_manager_directory': 'snapshot_id,manager_id,manager_name',
                'open_fund_concept_generation': 'generation_id,scope_count,fact_count,dictionary_count,'
                                                'eligible_count,undisclosed_count,not_applicable_count',
                'open_fund_concept_fact': 'generation_id,fund_code,concept_code,weight_nav,report_period,is_etf',
                'open_fund_concept_fund': 'generation_id,fund_code,batch_id,report_period,row_count',
                'open_fund_concept_dictionary': 'generation_id,concept_code,concept_name',
                'open_fund_disclosure_eligibility': 'version_id,product_id,fund_code,status,'
                                                  'evidence_sha256'}
            for table, columns in required.items():
                _sql(conn, 'SELECT '+columns+' FROM '+table+' WHERE 1=0', fetch=True)
            products = _sql(conn, "WITH newest AS (SELECT *,ROW_NUMBER() OVER(PARTITION BY product_id,"
                            "scope_kind,scope_key ORDER BY recorded_at DESC,batch_id DESC) AS rn "
                            "FROM open_fund_product_batch WHERE status='ready') "
                            "SELECT product_id,scope_kind,COUNT(*) AS scopes,MAX(recorded_at) AS observed_at,"
                            "SUM(row_count) AS attested_rows "
                            "FROM newest WHERE rn=1 GROUP BY product_id,scope_kind LIMIT 300",
                            fetch=True)
            scoped = _sql(conn, "SELECT product_id,LENGTH(scope_metadata_json) AS bytes,"
                          "CASE WHEN LENGTH(scope_metadata_json)<=4096 THEN scope_metadata_json "
                          "ELSE NULL END AS metadata "
                          "FROM open_fund_product_batch WHERE scope_kind='global' AND scope_metadata_json IS NOT NULL "
                          "ORDER BY recorded_at DESC,batch_id DESC LIMIT 300", fetch=True)
            latest_scope = {}
            for row in scoped:
                if row['product_id'] not in latest_scope:
                    if row['bytes'] > 4096:
                        raise ValueError('Invalid scope-metadata byte budget')
                    latest_scope[row['product_id']] = json.loads(row['metadata'])
            for row in products:
                row['dataset_scope'] = latest_scope.get(row['product_id'], {})
            # Readiness needs actual published facts; table existence is
            # reported independently and must not make an empty deployment green.
            global_status = any(row['product_id'] == 238 and row['scope_kind'] == 'global'
                                for row in products)
            for row in products:
                staged = row['product_id'] in {142, 143, 252, 263} and row['scope_kind'] == 'fund'
                row['stage'] = ('preparation' if staged
                                else 'read_cache')
                if global_status and row['product_id'] == 238 and row['scope_kind'] == 'fund':
                    row['stage'] = 'superseded'
            generations = _sql(conn, 'SELECT observed_at,scope_count,fact_count,eligible_count,'
                               'undisclosed_count,not_applicable_count '
                               'FROM open_fund_concept_generation ORDER BY observed_at DESC LIMIT 1', fetch=True)
            if generations:
                products.extend({'product_id': number, 'scope_kind': 'global', 'scopes': generations[0]['scope_count'],
                                 'observed_at': generations[0]['observed_at'],
                                 'attested_rows': generations[0]['fact_count'],
                                 'eligible_fund_count': generations[0]['eligible_count'],
                                 'undisclosed_fund_count': generations[0]['undisclosed_count'],
                                 'not_applicable_fund_count': generations[0]['not_applicable_count'],
                                 'stage': 'read_cache' if generations[0]['eligible_count'] else 'not_disclosed'}
                                for number in (263, 267))
            etfs = _sql(conn, 'SELECT * FROM (SELECT product_id,observed_at,scope_count,fact_count,'
                        'eligible_count,undisclosed_count,not_applicable_count,method_version,ROW_NUMBER() OVER('
                        'PARTITION BY product_id ORDER BY observed_at DESC,generation_id DESC) AS rn '
                        'FROM open_fund_etf_generation) latest WHERE rn=1', fetch=True)
            products.extend({'product_id': row['product_id'], 'scope_kind': 'global',
                             'observed_at': row['observed_at'], 'scopes': row['scope_count'],
                             'attested_rows': row['fact_count'],
                             'eligible_fund_count': row['eligible_count'],
                             'undisclosed_fund_count': row['undisclosed_count'],
                             'not_applicable_fund_count': row['not_applicable_count'],
                             'stage': 'read_cache' if (row['eligible_count']
                                                      and row['method_version'].endswith('v2-bound-immutable'))
                             else 'not_disclosed'} for row in etfs)
            base = _sql(conn, 'SELECT kind,scope_id,row_count,observed_at,window_start,window_end '
                        'FROM (SELECT *,ROW_NUMBER() OVER (PARTITION BY kind,scope_id '
                        'ORDER BY observed_at DESC,version_id DESC) AS rank_no FROM open_fund_base_watermark) latest '
                        'WHERE rank_no=1 ORDER BY observed_at DESC LIMIT 300', fetch=True)
            base_products = {'base-master': (233, 235, 236), 'base-categories': (237,), 'base-nav': (245, 246),
                             'base-dividends': (256,), 'base-splits': (257,), 'base-holdings': (258, 261),
                             'base-tenures': (232, 239, 250), 'base-returns': (249,)}
            for item in base:
                products.extend({'product_id': number, 'scope_kind': item['scope_id'], 'scopes': 1,
                                 'observed_at': item['observed_at'], 'attested_rows': item['row_count'],
                                 'stage': 'read_cache',
                                 'window_start': item['window_start'], 'window_end': item['window_end']}
                                for number in base_products.get(item['kind'], ()) if item['row_count'] > 0
                                and (item['kind'] != 'base-holdings' or
                                     (number == 258 and item['scope_id'].endswith(':stock')) or
                                     (number == 261 and item['scope_id'].endswith(':bond'))))
            current = [row for row in products if row['stage'] == 'read_cache']
        return jsonify({"service": "ifund-open-data", "schemaVersion": 6, "database": "ok",
                        "ready": bool(current), "status": "ready" if current else "not_ready", "products": products,
                        "fresh_product_count": len({row["product_id"] for row in current}),
                        "coverage": "cached_verified_windows_only"}), (
                            200 if current else 503)
    except Exception:  # pylint: disable=broad-exception-caught
        return jsonify({"service": "ifund-open-data", "schemaVersion": 6, "ready": False, "status": "not_ready",
                        "database": "migration_or_connection_unavailable"}), 503


def _arguments(extra=(), *, require_code=True):
    allowed = _COMMON | set(extra)
    if set(request.args) - allowed or any(len(values) != 1 for _, values in request.args.lists()):
        raise ValueError("Unknown or repeated query parameter")
    code = request.args.get("fund_code")
    codes = request.args.get("fund_codes")
    if code is not None and codes is not None:
        raise ValueError("Provide fund_code or fund_codes, not both")
    selected = codes.split(",") if codes is not None else ([code] if code is not None else [])
    if (require_code and not selected) or len(selected) > 20 or any(not _CODE.fullmatch(item) for item in selected):
        raise ValueError("Provide one to twenty six-digit fund codes")
    code = ",".join(sorted(set(selected))) if selected else None
    try:
        page = int(request.args.get("page", "1"))
        page_size = int(request.args.get("page_size", "100"))
    except ValueError as exc:
        raise ValueError("page and page_size must be integers") from exc
    if not 1 <= page <= 100000 or not 1 <= page_size <= 500:
        raise ValueError("page or page_size is out of range")
    start, end = request.args.get("start_date"), request.args.get("end_date")
    for value in (start, end):
        if value is not None and (date.fromisoformat(value).isoformat() != value):
            raise ValueError("Dates must use YYYY-MM-DD")
    if start and end and start > end:
        raise ValueError("start_date must not be later than end_date")
    return code, start, end, page, page_size


def _filters(code, start=None, end=None, *, date_field="trade_date"):
    result = [("fund_code", f"in.({code})" if "," in code else f"eq.{code}")]
    if start:
        result.append((date_field, f"gte.{start}"))
    if end:
        result.append((date_field, f"lte.{end}"))
    return result


def _project(row, fields):
    return _jsonable({field: row.get(field) for field in fields})


def _jsonable(value):
    # MySQL DATE columns arrive as datetime.date; Flask's default serializer
    # emits RFC1123 ("Tue, 30 Jun ... GMT"), which is not the source contract.
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _page(table, filters, fields, order, page, page_size):
    require_base(table)
    params = [*filters, ("select", ",".join(fields)), ("order", order),
              ("limit", page_size), ("offset", (page - 1) * page_size)]
    with database.get_db().transaction(read_only=True):
        total = database.count(table, filters)
        items = database.select(table, params)
    return {"items": _jsonable(items), "total": total, "page": page, "page_size": page_size, "source": "ifund"}


def _rows_page(rows, page, size, **metadata):
    offset = (page - 1) * size
    return {"items": _jsonable(rows[offset:offset + size]), "total": len(rows), "page": page,
            "page_size": size, "source": "ifund", **metadata}


def _valid_amount(value):
    if isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value)) and float(value) >= 0
    except (TypeError, ValueError):
        return False


def _add_names(result):
    codes = sorted({row["fund_code"] for row in result["items"]})
    if codes:
        names = database.select("funds", {"code": f"in.({','.join(codes)})", "select": "code,name"})
        by_code = {row["code"]: row["name"] for row in names}
        for row in result["items"]:
            row["fund_name"] = by_code.get(row["fund_code"])
    return result


@bp.get("/fund/nav")
def nav_history():
    """Return independent unit, accumulated and adjusted NAV with exact pagination."""
    code, start, end, page, size = _arguments()
    with database.get_db().transaction(read_only=True):
        selected = set(code.split(","))
        masters = database.select("funds", {
            "code": f"in.({code})", "select": "code,type",
        })
        if ({row["code"] for row in masters} != selected
                or any(row.get("type") not in _UNIT_NAV_TYPES for row in masters)):
            # Reject the complete requested set before pagination; silently
            # dropping unsafe funds would falsify total and batch semantics.
            return jsonify({"error": "product_window_incomplete",
                            "reason": "nav_type_unverified"}), 503
        result = _page("fund_nav", _filters(code, start, end),
                       ("fund_code", "trade_date", "nav", "acc_nav", "daily_return", "adj_nav", "adj_src"),
                       "trade_date.asc,fund_code.asc", page, size)
        _add_names(result)
    if os.getenv('IFUND_OPEN_REQUIRE_BASE_WATERMARK', '0') == '1' and result['total'] == 0:
        return jsonify({'error': 'product_window_incomplete', 'reason': 'nav_window_not_observed'}), 503
    return jsonify(result)


def _funds(*, require_code):
    code, _, _, page, size = _arguments(require_code=require_code)
    if request.args.get("start_date") or request.args.get("end_date"):
        raise ValueError("Fund master does not support date filtering")
    filters = [("code", f"in.({code})" if "," in code else f"eq.{code}")] if code else []
    with database.get_db().transaction(read_only=True):
        result = _page("funds", filters, _MASTER, "code.asc", page, size)
        codes = [row["code"] for row in result["items"]]
        if codes:
            selected = [("fund_code", f"in.({','.join(codes)})")]
            details = database.select("fund_details", [*selected, ("select", ",".join(_DETAILS))])
            extensions = database.select("fund_basic_ext", selected)
            by_code = {row["fund_code"]: row for row in details}
            by_ext = {row["fund_code"]: row for row in extensions}
            result["items"] = [
                {**row, **_project(by_code.get(row["code"], {}), _DETAILS),
                 **_project(by_ext.get(row["code"], {}), _EXT), "fund_code": row["code"]}
                for row in result["items"]
            ]
    result["units"] = {"scale": "100m_CNY", "returns": "percent", "asset_ratios": "percent"}
    return jsonify(result)


@bp.get("/fund/basic-info")
def fund_basic():
    """Return only public master/detail/extension fields for one fund."""
    return _funds(require_code=True)


@bp.get("/fund/list")
def fund_list():
    """Return the complete public fund master with stable code ordering."""
    return _funds(require_code=False)


@bp.get("/fund/categories")
def fund_categories():
    """Return the actual iFund classification dictionary."""
    _, start, end, page, size = _arguments(require_code=False)
    if request.args.get("fund_code") or request.args.get("fund_codes") or start or end:
        raise ValueError("Categories support pagination only")
    return jsonify(_page("fund_types", [], ("type_name", "category"), "type_name.asc", page, size))


@bp.get("/fund/returns")
def fund_returns():
    """Return stored return snapshots, without relabeling them as manager returns."""
    code, start, end, page, size = _arguments()
    fields = ("fund_code", "trade_date", "return_1m", "return_3m", "return_6m", "return_1y",
              "return_3y", "return_5y", "return_ytd", "return_since_inception")
    with database.get_db().transaction(read_only=True):
        result = _add_names(_page("fund_details", _filters(code, start, end), fields, "fund_code.asc", page, size))
    result["units"] = {"returns": "percent"}
    return jsonify(result)


@bp.get("/fund/fees")
def fund_fees():
    """Return supported fee snapshots. Tiered subscription fees remain unavailable."""
    code, start, end, page, size = _arguments()
    if start or end:
        raise ValueError("Fee snapshots do not support historical dates")
    # sales_service_fee is an optional deployment migration; project it after
    # selecting the bounded master row so older databases remain readable.
    result = _page("fund_basic_ext", _filters(code), ("*",), "fund_code.asc", page, size)
    result["items"] = [_project(row, ("fund_code", "management_fee", "custodian_fee", "sales_service_fee"))
                       for row in result["items"]]
    result["coverage"] = "management_custodian_sales_service_fee_snapshot"
    return jsonify(result)


@bp.get("/fund/events")
def fund_events():
    """Return actual dividend or split events with exact date filtering."""
    code, start, end, page, size = _arguments({"event_type"})
    kind = request.args.get("event_type")
    if kind not in {"div", "split"}:
        raise ValueError("event_type must be div or split")
    result = _page("fund_div_split", [*_filters(code, start, end, date_field="ex_date"),
                                    ("event_type", f"eq.{kind}")],
                   ("*",), "ex_date.asc,event_type.asc,fund_code.asc", page, size)
    fields = ("fund_code", "ex_date", "event_type", "cash_per_unit", "split_ratio", "source",
              "ann_date", "record_date", "pay_date")
    result["items"] = [_project(row, fields) for row in result["items"]]
    result["coverage"] = "stored_events_only"
    result["units"] = {"cash_per_unit": "CNY_per_share", "split_ratio": "ratio"}
    return jsonify(result)


@bp.get("/fund/holdings")
def fund_holdings():
    """Read the requested report period exactly, never substitute a later quarter."""
    code, start, end, page, size = _arguments({"holding_type", "quarter"})
    kind = request.args.get("holding_type", "stock")
    quarter = request.args.get("quarter")
    if kind not in {"stock", "bond"} or (quarter is not None and not _QUARTER.fullmatch(quarter)):
        raise ValueError("holding_type must be stock or bond and quarter must be YYYYQ1..YYYYQ4")
    if start or end:
        raise ValueError("Holdings use a report quarter, not a date window")
    if quarter is None and "," in code:
        body, status = _batch_latest_holdings(code, kind, page, size)
        return jsonify(body), status
    if quarter is None:
        latest = database.select_one("fund_holdings", {"fund_code": f"eq.{code}",
                                                      "holding_type": f"eq.{kind}", "order": "quarter.desc"})
        quarter = latest["quarter"] if latest else ""
    result = _page("fund_holdings", [*_filters(code), ("quarter", f"eq.{quarter}"),
                                   ("holding_type", f"eq.{kind}")],
                   ("fund_code", "quarter", "holding_type", "asset_code", "asset_name",
                    "hold_ratio", "hold_amount", "hold_market_value"),
                   "hold_ratio.desc,asset_code.asc,fund_code.asc", page, size)
    result["coverage"] = "disclosed_holdings"
    result["units"] = {"hold_ratio": "percent", "hold_amount": "10k_shares", "hold_market_value": "10k_CNY"}
    result["quarter"] = quarter or None
    return jsonify(result)


def _batch_latest_holdings(codes, kind, page, size):
    """Each member keeps its latest disclosed quarter; globally count and page."""
    require_base("fund_holdings")
    from .product_store import _sql  # pylint: disable=import-outside-toplevel,protected-access
    fields = ("fund_code", "quarter", "holding_type", "asset_code", "asset_name",
              "hold_ratio", "hold_amount", "hold_market_value")
    selected = codes.split(",")
    placeholders = ",".join("?" for _ in selected)
    with database.get_db().transaction(read_only=True) as conn:
        latest = _sql(conn, "SELECT fund_code,MAX(quarter) AS quarter FROM fund_holdings "
                      "WHERE fund_code IN ("+placeholders+") AND holding_type=? GROUP BY fund_code",
                      (*selected, kind), fetch=True)
        if {row["fund_code"] for row in latest} != set(selected):
            return {"error": "product_window_incomplete", "reason": "batch_member_uncovered"}, 503
        cte = ("WITH latest AS (SELECT fund_code,MAX(quarter) AS quarter FROM fund_holdings WHERE "
               "fund_code IN ("+placeholders+") AND holding_type=? GROUP BY fund_code) ")
        where = " FROM fund_holdings h JOIN latest l ON h.fund_code=l.fund_code AND h.quarter=l.quarter "
        where += "WHERE h.holding_type=?"
        params = (*selected, kind, kind)
        total = _sql(conn, cte+"SELECT COUNT(*) AS total"+where, params, fetch=True)[0]["total"]
        rows = _sql(conn, cte+"SELECT "+",".join("h."+field for field in fields)+where+
                    " ORDER BY h.hold_ratio DESC,h.asset_code,h.fund_code LIMIT ? OFFSET ?",
                    (*params, size, (page-1)*size), fetch=True)
    return {"items": _jsonable(rows), "total": total, "page": page, "page_size": size, "source": "ifund",
            "coverage": "each_fund_latest_disclosed_holdings", "quarters": latest,
            "units": {"hold_ratio": "percent", "hold_amount": "10k_shares", "hold_market_value": "10k_CNY"}}, 200


@bp.get("/fund/manager-tenure")
def manager_tenure():
    """Return fund-level tenure segments and their actual percentage returns."""
    code, start, end, page, size = _arguments({"is_current", "manager_name"})
    if start or end:
        raise ValueError("Manager tenure does not support NAV date filtering")
    filters = _filters(code)
    current = request.args.get("is_current")
    if current is not None:
        if current not in {"0", "1"}:
            raise ValueError("is_current must be 0 or 1")
        filters.append(("is_current", f"eq.{current}"))
    name = request.args.get("manager_name")
    if name is not None:
        if not name.strip() or len(name) > 80:
            raise ValueError("manager_name must be one to eighty characters")
        filters.append(("managers", f"eq.{name}"))
    with database.get_db().transaction(read_only=True):
        result = _add_names(_page("fund_manager_tenure", filters,
                                 ("fund_code", "seq", "managers", "start_date", "end_date", "is_current",
                                  "tenure_days", "tenure_return"), "seq.asc,fund_code.asc", page, size))
    result["units"] = {"tenure_return": "percent"}
    return jsonify(result)


@bp.get("/fund/shares")
def fund_shares():
    """Return dated share snapshots, preserving the source's ten-thousand-share unit."""
    code, start, end, page, size = _arguments()
    return jsonify(_page("fund_share", _filters(code, start, end),
                         ("fund_code", "ts_code", "trade_date", "share_type", "share_value", "share_unit"),
                         "trade_date.asc,ts_code.asc,share_type.asc", page, size))


@bp.get("/fund/reference-categories")
def reference_categories():
    """Expose actual fund types for the reviewed public first-level taxonomy."""
    code, start, end, page, size = _arguments()
    if start or end:
        raise ValueError("Current classifications do not support historical dates")
    result = _page("funds", [("code", f"in.({code})")], ("code", "name", "type"), "code.asc", page, size)
    result["items"] = [{"fund_code": row["code"], "fund_name": row["name"], "type": row["type"]}
                       for row in result["items"]]
    result["coverage"] = "first_level_classification_from_funds_type"
    return jsonify(result)


@bp.get("/fund/reference-fees")
def reference_fees():
    """Return real fixed fee rows, never manufacture subscription fee tiers."""
    code, start, end, page, size = _arguments()
    if start or end:
        raise ValueError("Current fee snapshots have no verified publication date")
    require_base("fund_basic_ext")
    with database.get_db().transaction(read_only=True):
        masters = database.select("fund_basic_ext", _filters(code))
        rows = []
        for master in sorted(masters, key=lambda row: row["fund_code"]):
            if all(master.get(field) is None for field in ("management_fee", "custodian_fee", "sales_service_fee")):
                return jsonify({"error": "product_window_incomplete", "reason": "fee_facts_unavailable"}), 503
            for field, label in (("management_fee", "管理费"), ("custodian_fee", "托管费"),
                                 ("sales_service_fee", "销售服务费")):
                value = master.get(field)
                if value is None:
                    continue
                if not _valid_amount(value):
                    return jsonify({"error": "product_window_incomplete", "reason": "fee_value_unverified"}), 503
                rows.append({"fund_code": master["fund_code"], "fee_kind": label,
                             "fee_percent": value, "source": master.get("source")})
        result = _add_names(_rows_page(rows, page, size, units={"fee_percent": "percent"},
                                      coverage="fixed_management_custodian_sales_service_rates"))
    return jsonify(result)


@bp.get("/fund/reference-listings")
def reference_listings():
    """Read documented issue/open dates, par value and fees from the extension."""
    code, start, end, page, size = _arguments()
    if start or end:
        raise ValueError("Issue snapshots have no verified first publication date")
    fields = ("fund_code", "issue_date", "par_value", "purchase_start_date", "redemption_start_date",
              "management_fee", "custodian_fee")
    require_base("fund_basic_ext")
    with database.get_db().transaction(read_only=True):
        masters = database.select("fund_basic_ext", [*_filters(code), ("select", ",".join(fields)),
                                                   ("order", "fund_code.asc")])
        if any(all(row.get(field) is None for field in fields[1:]) for row in masters):
            return jsonify({"error": "product_window_incomplete", "reason": "issue_facts_unavailable"}), 503
        result = _add_names(_rows_page(masters, page, size, units={"fees": "percent"},
                                      coverage="issue_open_dates_par_value_and_fixed_rates"))
    return jsonify(result)


@bp.get("/fund/reference-shares")
def reference_shares():
    """Return canonical end-point total shares with explicit unit provenance."""
    code, start, end, page, size = _arguments()
    filters = [*_filters(code, start, end), ("share_type", "eq.fund_total"), ("source_field", "eq.fd_share")]
    with database.get_db().transaction(read_only=True):
        masters = database.select("fund_basic_ext", [*_filters(code), ("select", "fund_code,ts_code")])
        if {row["fund_code"] for row in masters} != set(code.split(",")):
            return jsonify({"error": "product_window_incomplete", "reason": "share_role_unverified"}), 503
        # A six-digit code can have OF and exchange aliases. Use the unique
        # canonical extension role, never sum the two or discard their suffix
        # only after projecting them into indistinguishable public records.
        roles = sorted({row["ts_code"] for row in masters})
        filters.append(("ts_code", f"in.({','.join(roles)})"))
        if database.count("fund_share", [*filters, ("share_unit", "neq.10k_shares")]):
            return jsonify({"error": "product_window_incomplete", "reason": "share_unit_unverified"}), 503
        result = _add_names(_page("fund_share", filters,
                                 ("fund_code", "ts_code", "trade_date", "share_type", "share_value",
                                  "share_unit", "source_field"),
                                 "trade_date.asc,fund_code.asc,ts_code.asc", page, size))
        if any(not _valid_amount(row["share_value"]) for row in result["items"]):
            return jsonify({"error": "product_window_incomplete", "reason": "share_value_unverified"}), 503
    result["coverage"] = "canonical_role_point_in_time_total_shares_only"
    result["units"] = {"share_value": "10k_shares"}
    return jsonify(result)


@bp.get("/fund/reference-performance")
def reference_performance():
    """Expose observed return/risk snapshots without inventing attribution."""
    code, start, end, page, size = _arguments()
    if start or end:
        raise ValueError("Risk snapshots have no verified historical measurement cutoff")
    periods = ("since_inception", "ytd", "1m", "3m", "6m", "1y", "3y", "5y")
    fields = ("fund_code", *(f"return_{period}" for period in periods),
              *(f"{metric}_{period}" for metric in ("volatility", "sharpe", "max_drawdown")
                for period in ("1y", "3y", "5y")))
    with database.get_db().transaction(read_only=True):
        snapshots = database.select("fund_details", [*_filters(code), ("select", ",".join(fields)),
                                                     ("order", "fund_code.asc")])
        if any(all(row.get(field) is None for field in fields[1:]) for row in snapshots):
            return jsonify({"error": "product_window_incomplete", "reason": "risk_facts_unavailable"}), 503
        result = _add_names(_rows_page(snapshots, page, size))
    result["coverage"] = "observed_return_annual_volatility_sharpe_and_drawdown_only"
    result["units"] = {"returns": "percent", "volatility": "percent", "sharpe": "ratio",
                       "max_drawdown": "percent"}
    return jsonify(result)
