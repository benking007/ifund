"""AI 定性分析蓝图：流式分析 + 提示词管理。"""
from __future__ import annotations

import asyncio
import json
import logging

from flask import Blueprint, Response, jsonify, request, stream_with_context

from flask_jwt_extended import jwt_required

from .service import (
    analyze_fund_streaming,
    get_prompt,
    set_prompt,
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_USER_PROMPT_TEMPLATE,
)

logger = logging.getLogger(__name__)

bp = Blueprint("ai_analyze", __name__, url_prefix="/api/fund")


@bp.post("/<code>/ai-analyze")
@jwt_required()
def ai_analyze(code):
    """流式 AI 分析：返回 SSE 事件流。"""
    def generate():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            agen = analyze_fund_streaming(code)
            while True:
                try:
                    item = loop.run_until_complete(agen.__anext__())
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                except StopAsyncIteration:
                    break
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    logger.exception("AI analyze streaming error for %s", code)
                    yield f"data: {json.dumps({'type': 'error', 'detail': str(exc)})}\n\n"
                    break
            loop.close()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.exception("AI analyze setup error for %s", code)
            yield f"data: {json.dumps({'type': 'error', 'detail': str(exc)})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@bp.get("/ai-prompt")
@jwt_required()
def get_ai_prompt():
    """获取 AI 分析提示词。"""
    return jsonify({
        "system": get_prompt("ai_analyze_system", DEFAULT_SYSTEM_PROMPT),
        "user": get_prompt("ai_analyze_user", DEFAULT_USER_PROMPT_TEMPLATE),
    })


@bp.put("/ai-prompt")
@jwt_required()
def update_ai_prompt():
    """更新 AI 分析提示词。"""
    data = request.get_json(silent=True) or {}
    if "system" in data:
        set_prompt("ai_analyze_system", data["system"])
    if "user" in data:
        set_prompt("ai_analyze_user", data["user"])
    return jsonify({"ok": True})
