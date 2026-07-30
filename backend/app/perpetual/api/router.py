"""永续组合 API：/api/perpetual/run + /api/perpetual/replay。"""
from __future__ import annotations

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from app import db as database
from app.perpetual.algo import loader, pipeline, replay

bp = Blueprint("perpetual", __name__, url_prefix="/api/perpetual")


def build_result(codes: list[str] | None = None, diagnose: list[str] | None = None,
                 include_cloud: bool = False, as_of: str | None = None) -> dict:
    """共享入口：加载数据 + 跑引擎（HTTP 与 CLI 复用）。"""
    universe = loader.load_universe(codes)
    nav_by_code = {f["code"]: loader.load_nav_series(f["code"], as_of) for f in universe}
    tenure_by_code = {f["code"]: loader.current_tenure_days(f["code"], as_of) for f in universe}
    holdings_by_code = {f["code"]: loader.load_quarter_holdings(f["code"], as_of) for f in universe}
    return pipeline.run(universe, nav_by_code, tenure_by_code, holdings_by_code,
                        diagnose_codes=diagnose, include_cloud=include_cloud, as_of=as_of)


def _preset_codes(preset_id: int, user_id: int) -> list[str] | None:
    """从预设镜像取候选代码列表；无预设返回 None（全库）。"""
    if not preset_id:
        return None
    snap = database.select_one("fund_snapshots",
                               {"user_id": f"eq.{user_id}", "preset_id": f"eq.{preset_id}"})
    if not snap:
        return []
    import json
    items = json.loads(snap.get("items_json") or "[]")
    return [it["code"] for it in items if it.get("code")]


@bp.post("/run")
@jwt_required()
def run_perpetual():
    """生成永续组合。"""
    from app.preset_access import current_user_id
    data = request.get_json(silent=True) or {}
    user_id = current_user_id()
    preset_id = data.get("preset_id")
    as_of = data.get("as_of")
    diagnose = data.get("diagnose")
    if as_of and len(as_of) != 10:
        return jsonify({"error": "as_of 格式须为 YYYY-MM-DD"}), 400
    codes = _preset_codes(preset_id, user_id) if preset_id else None
    if codes is not None and not codes:
        return jsonify({"error": "预设无镜像快照或镜像为空"}), 422
    result = build_result(codes, diagnose, include_cloud=True, as_of=as_of)
    if "error" in result:
        return jsonify(result), 422
    return jsonify(result)


@bp.post("/replay")
@jwt_required()
def run_replay():
    """定期重筛回放。"""
    from app.preset_access import current_user_id
    data = request.get_json(silent=True) or {}
    user_id = current_user_id()
    start = data.get("start")
    if not start:
        return jsonify({"error": "start 必填（YYYY-MM-DD）"}), 400
    preset_id = data.get("preset_id")
    codes = _preset_codes(preset_id, user_id) if preset_id else None
    if codes is not None and not codes:
        return jsonify({"error": "预设无镜像快照或镜像为空"}), 422
    result = replay.run_replay(
        codes=codes,
        start=start,
        step_months=data.get("step_months", 6),
        keep_rank=data.get("keep_rank", 20),
        max_replace=data.get("max_replace", 3),
    )
    if "error" in result:
        return jsonify(result), 422
    return jsonify(result)


@bp.post("/save")
@jwt_required()
def save_portfolio():
    """持久化一次生成结果。"""
    import json as _json
    from app.preset_access import current_user_id
    data = request.get_json(silent=True) or {}
    user_id = current_user_id()
    result = data.get("result")
    if not result:
        return jsonify({"error": "result 必填"}), 400
    row = database.insert("perpetual_portfolio", {
        "user_id": user_id,
        "preset_id": data.get("preset_id"),
        "as_of": data.get("as_of"),
        "result_json": _json.dumps(result, ensure_ascii=False),
    })
    return jsonify({"id": row["id"]})


@bp.get("/latest")
@jwt_required()
def latest_portfolio():
    """取最近一次保存的结果。"""
    import json as _json
    from app.preset_access import current_user_id
    user_id = current_user_id()
    row = database.select_one("perpetual_portfolio", {
        "user_id": f"eq.{user_id}", "order": "created_at.desc",
    })
    if not row:
        return jsonify(None)
    return jsonify({
        "id": row["id"],
        "preset_id": row["preset_id"],
        "as_of": row["as_of"],
        "created_at": row["created_at"],
        "result": _json.loads(row["result_json"]),
    })
