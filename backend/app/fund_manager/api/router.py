"""基金经理 API：/api/fund_manager。"""
from __future__ import annotations

import json
import os
from pathlib import Path

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from app import db as database
from app.fund_manager.crud import manager_crud

bp = Blueprint("fund_manager", __name__, url_prefix="/api/fund_manager")

_WORKER_SCRIPT = str(Path(__file__).resolve().parents[1] / "fetch" / "worker.py")


@bp.post("/sync")
@jwt_required()
def sync():
    """触发经理数据采集任务。"""
    from app.common.sync_launcher import start_sync_task
    from app.common.task_support import get_running
    if get_running("fetch_fund_manager"):
        return jsonify({"detail": "已有运行中的任务"}), 409
    codes = None
    preset_id = request.args.get("presetId", type=int)
    if preset_id:
        snap = database.select_one("fund_snapshots", {"preset_id": f"eq.{preset_id}"})
        if snap:
            items = json.loads(snap.get("items_json") or "[]")
            codes = [it["code"] for it in items if it.get("code")]
    task_id = start_sync_task("fetch_fund_manager", _WORKER_SCRIPT, codes=codes)
    return jsonify({"task_id": task_id})


@bp.get("/task/running")
def task_running():
    """查询当前是否有运行中的采集任务。"""
    from app.common.task_support import get_running
    task = get_running("fetch_fund_manager")
    return jsonify(task)


@bp.post("/task/<int:task_id>/terminate")
@jwt_required()
def terminate_endpoint(task_id):
    """终止指定任务。"""
    from app.common.task_runner import terminate_task as _kill
    _kill(task_id)
    return jsonify({"ok": True})


@bp.get("/list")
@jwt_required()
def list_managers():
    """分页列表。"""
    keyword = request.args.get("keyword", "")
    coverage = request.args.get("coverage", "all")
    preset_id = request.args.get("presetId", type=int)
    page = max(1, request.args.get("page", 1, type=int))
    page_size = min(200, request.args.get("pageSize", 50, type=int))
    sort_field = request.args.get("sortField", "code")
    sort_order = request.args.get("sortOrder", "asc")
    total, items = manager_crud.list_summary(
        keyword=keyword, coverage=coverage, preset_id=preset_id,
        skip=(page - 1) * page_size, limit=page_size,
        order_field=sort_field, order_dir=sort_order,
    )
    return jsonify({"total": total, "items": items})


@bp.get("/tenure/<code>")
@jwt_required()
def tenure(code):
    """单只基金完整任职史。"""
    rows = manager_crud.get_tenure(code)
    return jsonify(rows)


@bp.get("/stats")
@jwt_required()
def stats():
    """覆盖率统计。"""
    return jsonify(manager_crud.coverage_stats())
