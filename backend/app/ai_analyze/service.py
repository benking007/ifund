"""AI 定性分析服务：调用 Qoder Agent SDK 对单只基金做历史穿透分析（支持流式）。"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import re
from collections.abc import AsyncIterator
from pathlib import Path

from app import db as database

logger = logging.getLogger(__name__)

# 默认提示词的「单一真相源」= prompts/*.md（版本控制、可 git diff、全新项目自带）。
# app_settings 里的同名 key 只是运行时可选覆盖；删除覆盖即回落到这两个文件。
# 这样避免了「UI 抽屉里改的提示词只落 DB、换机器/重建库就丢、回落到过时硬编码」的漂移。
_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"

# 文件缺失时的最小兜底：不让打包疏漏拖垮整个后端启动，但会在日志里报出来。
_FALLBACK_SYSTEM = "你是基金定性分析助手，只输出一个严格匹配字段定义的 JSON 对象。"
_FALLBACK_USER = "请对以下基金做历史穿透定性分析并输出 JSON：\n\n__BUNDLE_JSON__"


def _load_prompt_file(name: str, fallback: str) -> str:
    """从 prompts/ 读取提示词文件；缺失时回落到最小兜底并告警。"""
    try:
        return (_PROMPT_DIR / name).read_text(encoding="utf-8")
    except OSError:
        logger.error("提示词文件缺失：%s，已回落到最小兜底", _PROMPT_DIR / name)
        return fallback


DEFAULT_SYSTEM_PROMPT = _load_prompt_file("system.md", _FALLBACK_SYSTEM)
DEFAULT_USER_PROMPT_TEMPLATE = _load_prompt_file("user.md", _FALLBACK_USER)


def get_prompt(key: str, default: str) -> str:
    """读提示词：app_settings 覆盖优先，否则用 default（= prompts/*.md 文件默认值）。"""
    row = database.select_one("app_settings", {"key": f"eq.{key}"})
    return row["value"] if row else default


def set_prompt(key: str, value: str) -> None:
    """写入/更新 app_settings 中的提示词覆盖。"""
    exists = database.select_one("app_settings", {"key": f"eq.{key}"})
    if exists:
        database.update("app_settings", {"key": key}, {"value": value})
    else:
        database.insert("app_settings", {"key": key, "value": value})


def reset_prompt(key: str) -> None:
    """删除 app_settings 中的覆盖，使该提示词回落到 prompts/*.md 文件默认值。"""
    database.delete("app_settings", {"key": f"eq.{key}"})


def _extract_json(text: str) -> dict:
    """从 AI 回复文本中提取 JSON 对象（兼容 markdown 代码块包裹）。"""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    m2 = re.search(r"\{.*\}", text, re.DOTALL)
    if m2:
        return json.loads(m2.group(0))
    return json.loads(text)


_AI_ENUMS = {
    "luck_verdict": {"solid", "mixed", "luck"},
    "concentration": {"single_bet", "focused", "diversified"},
    "fund_kind": {"subjective", "rotation", "sector"},
    "scale_risk": {"tiny", "small", "ok", "large"},
    "style_stability": {"stable", "volatile", "unproven"},
    "confidence": {"high", "medium", "low"},
}
_AI_INT_RANGE = {
    "rating": (0, 3), "recommend": (0, 1), "skill_score": (0, 100),
    "is_original": (0, 1), "is_comanaged": (0, 1),
}
_AI_TEXT = {"manager", "verdict", "skill_reason", "concentration_reason", "hard_thesis",
            "turnover_note", "model", "data_basis", "analyzed_at"}
_AI_FLOAT = {"tenure_years"}


def _coerce_field(key: str, val):
    if key in _AI_ENUMS:
        if val not in _AI_ENUMS[key]:
            raise ValueError(f"{key} 取值须为 {sorted(_AI_ENUMS[key])}，收到 {val!r}")
        return val
    if key in _AI_INT_RANGE:
        lo, hi = _AI_INT_RANGE[key]
        iv = int(bool(val)) if isinstance(val, bool) else int(val)
        if not lo <= iv <= hi:
            raise ValueError(f"{key} 须在 [{lo},{hi}]，收到 {iv}")
        return iv
    if key in _AI_FLOAT:
        return float(val)
    if key == "tags":
        if isinstance(val, str):
            val = [val]
        if not isinstance(val, list):
            raise ValueError("tags 须为字符串数组")
        return json.dumps([str(t) for t in val], ensure_ascii=False)
    if key in _AI_TEXT:
        return None if val is None else str(val)
    raise ValueError(f"未知字段 {key!r}")


def _save_result(code: str, payload: dict) -> dict:
    now = datetime.datetime.now().isoformat(timespec="seconds")
    fields = {}
    for k, v in payload.items():
        if k in ("model", "data_basis"):
            fields[k] = _coerce_field(k, v)
        elif k in _AI_ENUMS or k in _AI_INT_RANGE or k in _AI_FLOAT or k in _AI_TEXT or k == "tags":
            fields[k] = _coerce_field(k, v)
    fields["updated_at"] = now
    fields.setdefault("analyzed_at", now)

    exists = database.select_one("fund_ai_analysis", {"fund_code": f"eq.{code}"})
    if exists:
        database.update("fund_ai_analysis", {"fund_code": code}, fields)
    else:
        database.insert("fund_ai_analysis", {"fund_code": code, **fields})
    return database.select_one("fund_ai_analysis", {"fund_code": f"eq.{code}"})


async def _stream_sdk(bundle: dict, system_prompt: str, user_template: str) -> AsyncIterator[str]:
    # pylint: disable=import-outside-toplevel
    from qoder_agent_sdk import (
        AssistantMessage,
        QoderAgentOptions,
        ResultMessage,
        TextBlock,
        access_token_from_env,
        query,
    )

    # cli_path 从环境变量取（默认走 PATH 里的 qoder），避免写死某台机器的绝对路径。
    opts = QoderAgentOptions(
        auth=access_token_from_env(),
        system_prompt=system_prompt,
        permission_mode="bypassPermissions",
        max_turns=1,
        cli_path=os.getenv("QODER_CLI_PATH", "qoder"),
    )
    bundle_json = json.dumps(bundle, ensure_ascii=False, separators=(",", ":"))
    user_prompt = user_template.replace("__BUNDLE_JSON__", bundle_json)

    async for msg in query(prompt=user_prompt, options=opts):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    yield block.text
        elif isinstance(msg, ResultMessage):
            if msg.is_error:
                errors = msg.errors or ["unknown error"]
                raise RuntimeError(f"SDK error: {'; '.join(errors)}")


async def analyze_fund_streaming(code: str) -> AsyncIterator[dict]:
    """流式分析：yield {"type": "chunk", "text": ...} 和最终 {"type": "done", "ai": {...}}。"""
    # pylint: disable=import-outside-toplevel
    from cli.bundle import build_bundle

    bundle = build_bundle(code)
    if not bundle:
        raise ValueError(f"基金 {code} 不存在或无数据")

    system_prompt = get_prompt("ai_analyze_system", DEFAULT_SYSTEM_PROMPT)
    user_template = get_prompt("ai_analyze_user", DEFAULT_USER_PROMPT_TEMPLATE)

    logger.info("AI analyze streaming: starting for %s", code)
    full_text = ""
    async for chunk in _stream_sdk(bundle, system_prompt, user_template):
        full_text += chunk
        yield {"type": "chunk", "text": chunk}

    logger.info("AI analyze streaming: done for %s (%d chars)", code, len(full_text))
    payload = _extract_json(full_text)
    row = _save_result(code, payload)
    ai_public = {k: v for k, v in row.items() if k not in ("id", "fund_code")}
    yield {"type": "done", "ai": ai_public}


def analyze_fund(code: str) -> dict:
    """同步分析（兼容旧调用）。"""
    results = []
    async def collect():
        async for item in analyze_fund_streaming(code):
            results.append(item)
    asyncio.run(collect())
    for r in results:
        if r.get("type") == "done":
            return r["ai"]
    raise RuntimeError("分析未完成")
