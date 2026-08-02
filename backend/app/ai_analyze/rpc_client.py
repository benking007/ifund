"""agim RPC 客户端：通过 Unix socket 调用 agim 的 llm_complete 工具。"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 120.0


TOOL_PATH = "/rpc/mcp__agim__llm_complete"


def _socket_path() -> Path:
    """读取当前 RPC socket 配置，避免模块 import 时冻结环境变量。"""
    return Path(os.getenv("AGIM_RPC_SOCKET", os.path.expanduser("~/.agim/rpc.sock")))


def _rpc_token() -> str:
    """读取当前 RPC token，支持服务启动后注入或轮换密钥。"""
    return os.getenv("AGIM_RPC_TOKEN", "")


def _default_backend() -> str:
    """读取当前默认模型配置。"""
    return os.getenv("IFUND_LLM_BACKEND", "deepseek-v4-flash")


async def llm_complete(
    messages: list[dict],
    *,
    system: str | None = None,
    backend: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout_ms: int | None = None,
) -> str:
    """调用 agim RPC 的 mcp__agim__llm_complete 并返回 result.text。

    Raises:
        RuntimeError: 环境变量缺失 / 认证失败 / 工具未放行 / 连接不可达 / RPC 返回错误。
    """
    rpc_token = _rpc_token()
    if not rpc_token:
        logger.error("AGIM_RPC_TOKEN 未设置，拒绝调用 agim RPC")
        raise RuntimeError(
            "AGIM_RPC_TOKEN 环境变量未设置，无法调用 agim RPC"
        )

    socket_path = _socket_path()
    timeout = (timeout_ms / 1000.0) if timeout_ms else _DEFAULT_TIMEOUT_S

    body: dict = {
        "args": {
            "backend": backend or _default_backend(),
            "messages": messages,
        },
    }
    if system is not None:
        body["args"]["system"] = system
    if temperature is not None:
        body["args"]["temperature"] = temperature
    if max_tokens is not None:
        body["args"]["maxTokens"] = max_tokens
    if timeout_ms is not None:
        body["args"]["timeoutMs"] = timeout_ms

    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")

    transport = httpx.AsyncHTTPTransport(uds=str(socket_path))
    async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
        try:
            resp = await client.post(
                f"http://localhost{TOOL_PATH}",
                content=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Agim-Rpc-Token": rpc_token,
                },
            )
        except httpx.ConnectError as exc:
            logger.warning("agim RPC 不可达：socket=%s，错误=%s", socket_path, exc)
            raise RuntimeError(
                f"agim RPC 不可达: socket={socket_path}, error={exc}"
            ) from exc
        except httpx.TimeoutException as exc:
            logger.warning("agim RPC 超时：socket=%s，timeout=%ss", socket_path, timeout)
            raise RuntimeError(
                f"agim RPC 超时: {timeout}s"
            ) from exc

    if resp.status_code == 401:
        logger.error("agim RPC 认证失败：HTTP 401")
        raise RuntimeError("agim RPC token 无效")
    if resp.status_code == 403:
        logger.error("agim RPC 工具未放行：HTTP 403")
        raise RuntimeError(
            "agim RPC 未放行 llm_complete: 检查 AGIM_RPC_ALLOWED_TOOLS"
        )
    if resp.status_code != 200:
        logger.error("agim RPC 返回异常状态码：HTTP %s", resp.status_code)
        raise RuntimeError(
            f"agim RPC 返回非预期状态码 {resp.status_code}: {resp.text[:500]}"
        )

    try:
        data = resp.json()
    except json.JSONDecodeError as exc:
        logger.error("agim RPC 返回非 JSON：%s", exc)
        raise RuntimeError(
            f"agim RPC 返回非 JSON: {resp.text[:500]}"
        ) from exc

    ok = data.get("ok", False)
    if not ok:
        error_msg = data.get("error", "unknown error")
        logger.error("agim RPC 调用失败：%s", error_msg)
        raise RuntimeError(f"agim RPC 调用失败: {error_msg}")

    result = data.get("result", {})
    text = result.get("text", "")
    if not isinstance(text, str):
        raise RuntimeError(
            f"agim RPC result.text 非字符串类型: {type(text).__name__}"
        )
    return text
