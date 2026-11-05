"""Authenticated, bounded read models. These endpoints never fetch or write data."""
from __future__ import annotations

from datetime import date, datetime
import math
import re

from flask import Blueprint, jsonify, request
from flask_jwt_extended import verify_jwt_in_request

from app import db as database
from .product_routes import register_product_routes

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
_UNIT_NAV_TYPES = frozenset({
    "股票型", "混合型", "混合型-偏股", "混合型-偏债", "混合型-灵活", "混合型-股债平衡",
    "债券型", "债券型-长债", "债券型-中短债", "债券型-利率债", "债券型-信用债",
    "债券型-混合一级", "债券型-混合二级", "指数型-股票", "指数型-固收",
    "FOF-稳健型", "FOF-均衡型", "FOF-进取型", "REITs",
    "QDII-混合偏股", "QDII-普通股票", "QDII-纯债", "QDII-混合灵活", "QDII-混合债",
    "QDII-商品", "QDII-FOF", "QDII-混合平衡", "QDII-REITs",
})


@bp.before_request
def authenticate():
    """Reuse the existing PAT exchange and short-lived JWT authentication."""
    verify_jwt_in_request()


@bp.errorhandler(ValueError)
def invalid_request(exc):
    """Return validation errors without database or deployment details."""
    return jsonify({"error": "invalid_request", "message": str(exc)}), 400


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
        raise ValueError("Batch holdings require an explicit quarter")
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
