"""Tushare 公募基金接口的轻量 HTTP 客户端。

这里只封装协议和字段映射，不依赖 tushare Python SDK，便于 worker 进程和测试
在没有 SDK 时运行。Token 只在请求体中使用，日志中不输出 token。

调用前会在 fin-data 的跨进程状态文件上预留请求槽，确保 iFund 与财务回填
共用同一个 Tushare 总闸。iFund 默认最多 20 次/分钟，遇到频限时会沿用
fin-data 的 100 → 50 → 20 档位降速和退避策略。
"""

from __future__ import annotations

# Mapping resolution is lazy; the apparent package cycle is not executed at import time.
# pylint: disable=cyclic-import

import fcntl
import json
import math
import os
import threading
import time
from pathlib import Path

import requests

from app.common.network import HTTP_TIMEOUT


ENDPOINT = os.getenv("TUSHARE_ENDPOINT", "http://api.tushare.pro")
DEFAULT_ENV_FILE = Path(
    os.getenv(
        "TUSHARE_TOKEN_FILE",
        "/root/workspace/ai-agent-platform/services/fin_data/.env",
    )
)
DEFAULT_LIMITER_STATE = "/tmp/fin-data-tushare-global-rate.json"
DEFAULT_GLOBAL_RATE_PER_MIN = 100.0
DEFAULT_CLIENT_RATE_PER_MIN = 20.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 2.0
DEFAULT_MAX_BACKOFF_SECONDS = 60.0
REQUEST_TIMEOUT = HTTP_TIMEOUT


class TushareError(RuntimeError):
    """Tushare 返回错误或响应格式异常。"""


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1].strip()
    return value


def _token_from_file(path: Path = DEFAULT_ENV_FILE) -> str:
    """从指定 .env 读取 token；只返回 token，不打印或记录。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "TUSHARE_TOKEN":
            return _strip_quotes(value.split(" #", 1)[0])
    return ""


def get_token() -> str:
    """读取 Tushare token，fin-data 项目文件优先于继承环境。

    systemd/Agent shell 可能继承失效的 ``TUSHARE_TOKEN``；让服务端正在使用的
    token 文件优先，可使 iFund 与 fin-data 始终使用同一凭据。文件不存在时
    才回退环境变量。``TUSHARE_PREFER_FIN_DATA`` 保留为向后兼容开关。
    """
    env_token = _strip_quotes(os.getenv("TUSHARE_TOKEN", ""))
    file_token = _token_from_file()
    return file_token or env_token


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


class _SharedGlobalLimiter:
    """与 fin-data ``GlobalAdaptiveLimiter`` 兼容的跨进程请求槽。"""

    def __init__(self) -> None:
        self.rate_per_min = _env_float(
            "IFUND_TUSHARE_RATE_PER_MIN", DEFAULT_CLIENT_RATE_PER_MIN
        )
        self.baseline_rate_per_min = _env_float(
            "FIN_DATA_TUSHARE_GLOBAL_RATE_PER_MIN", DEFAULT_GLOBAL_RATE_PER_MIN
        )
        if not math.isfinite(self.rate_per_min) or self.rate_per_min <= 0:
            raise ValueError("IFUND_TUSHARE_RATE_PER_MIN 必须为正数")
        if (
            not math.isfinite(self.baseline_rate_per_min)
            or self.baseline_rate_per_min <= 0
        ):
            raise ValueError("FIN_DATA_TUSHARE_GLOBAL_RATE_PER_MIN 必须为正数")
        self.state_path = Path(
            os.getenv("FIN_DATA_TUSHARE_LIMITER_STATE", DEFAULT_LIMITER_STATE)
        )
        self.lock_path = self.state_path.with_suffix(self.state_path.suffix + ".lock")

    def _default_state(self) -> dict[str, float | int]:
        return {
            "version": 1,
            "baseline_rate_per_min": self.baseline_rate_per_min,
            "current_rate_per_min": self.baseline_rate_per_min,
            "next_at": 0.0,
            "recover_at": 0.0,
            "rate_limit_count": 0,
            "updated_at": 0.0,
        }

    def _read_state(self) -> dict:
        state = self._default_state()
        try:
            loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state.update(loaded)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        state["baseline_rate_per_min"] = self.baseline_rate_per_min
        current = float(state.get("current_rate_per_min") or self.baseline_rate_per_min)
        state["current_rate_per_min"] = min(current, self.baseline_rate_per_min)
        return state

    def _write_state(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(
            f".{self.state_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self.state_path)

    def _locked_update(self, callback):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                state = self._read_state()
                result = callback(state)
                self._write_state(state)
                return result
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _tiers(self) -> list[float]:
        tiers = [
            self.baseline_rate_per_min,
            self.baseline_rate_per_min / 2.0,
            20.0,
        ]
        return sorted(
            {max(1.0, min(self.baseline_rate_per_min, value)) for value in tiers},
            reverse=True,
        )

    def _next_lower(self, current: float) -> float:
        return next(
            (tier for tier in self._tiers() if tier < current - 1e-9),
            self._tiers()[-1],
        )

    def _next_higher(self, current: float) -> float:
        return next(
            (tier for tier in reversed(self._tiers()) if tier > current + 1e-9),
            self.baseline_rate_per_min,
        )

    def wait(self) -> float:
        """原子预留一个全局请求槽，并等待到该时刻。"""

        def reserve(state: dict) -> float:
            now = time.time()
            current = float(state["current_rate_per_min"])
            recovery_after = _env_float(
                "FIN_DATA_TUSHARE_RATE_RECOVERY_SECONDS", 1800.0
            )
            if current < self.baseline_rate_per_min and now >= float(
                state.get("recover_at") or 0.0
            ):
                current = self._next_higher(current)
                state["current_rate_per_min"] = current
                state["recover_at"] = now + recovery_after
            effective_rate = min(self.rate_per_min, current)
            slot = max(now, float(state.get("next_at") or 0.0))
            state["next_at"] = slot + 60.0 / effective_rate
            state["updated_at"] = now
            return max(0.0, slot - now)

        wait_seconds = self._locked_update(reserve)
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        return wait_seconds

    def on_rate_limit(self, attempt: int, error: BaseException) -> float:
        """降低共享速率档位，并把下一请求推迟到退避窗口之后。"""
        backoff = min(
            _env_float("FIN_DATA_TUSHARE_BACKOFF_MAX", 480.0),
            _env_float("FIN_DATA_TUSHARE_BACKOFF_BASE", 60.0) * (2 ** max(0, attempt)),
        )

        def reduce(state: dict) -> None:
            now = time.time()
            current = float(state["current_rate_per_min"])
            effective = min(current, self.rate_per_min)
            state["current_rate_per_min"] = min(current, self._next_lower(effective))
            state["recover_at"] = now + _env_float(
                "FIN_DATA_TUSHARE_RATE_RECOVERY_SECONDS", 1800.0
            )
            state["next_at"] = max(float(state.get("next_at") or 0.0), now + backoff)
            state["rate_limit_count"] = int(state.get("rate_limit_count") or 0) + 1
            state["updated_at"] = now
            state["last_rate_limit_error"] = str(error)[:500]

        self._locked_update(reduce)
        return backoff


def _get_limiter() -> _SharedGlobalLimiter:
    return _SharedGlobalLimiter()


def _is_rate_limit(error: BaseException) -> bool:
    message = str(error)
    lowered = message.lower()
    return (
        "频率超限" in message
        or "限频" in message
        or "rate limit" in lowered
        or "retry shortly" in lowered
    )


def rows_from_response(response: dict) -> list[dict]:
    """把 Tushare ``fields/items`` 响应转换为字典行。"""
    if not isinstance(response, dict):
        raise TushareError("Tushare response is not an object")
    if response.get("code", 0) not in (0, "0", None):
        raise TushareError(str(response.get("msg") or "Tushare request failed"))
    data = response.get("data") or {}
    fields = data.get("fields") or []
    items = data.get("items") or []
    if not isinstance(fields, list) or not isinstance(items, list):
        raise TushareError("Tushare response has invalid data fields/items")
    return [
        dict(zip(fields, item)) for item in items if isinstance(item, (list, tuple))
    ]


def call(
    api_name: str,
    params: dict[str, object] | None = None,
    fields: str = "",
    *,
    timeout: tuple[int, int] = REQUEST_TIMEOUT,
) -> list[dict]:
    """经共享总闸调用 Tushare JSON-RPC 接口并返回字典行。"""
    token = get_token()
    if not token:
        raise TushareError("TUSHARE_TOKEN 未配置（环境变量或 fin-data/.env）")
    payload = {
        "api_name": api_name,
        "token": token,
        "params": params or {},
        "fields": fields,
    }
    limiter = _get_limiter()
    attempts = max(1, _env_int("IFUND_TUSHARE_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS))
    for attempt in range(attempts):
        limiter.wait()
        try:
            response = requests.post(ENDPOINT, json=payload, timeout=timeout)
            response.raise_for_status()
            rows = rows_from_response(response.json())
            return rows
        except TushareError as exc:
            if not _is_rate_limit(exc) or attempt + 1 >= attempts:
                raise
            limiter.on_rate_limit(attempt, exc)
        except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
            if attempt + 1 >= attempts:
                raise TushareError(f"Tushare {api_name} request failed: {exc}") from exc
            time.sleep(
                min(
                    DEFAULT_MAX_BACKOFF_SECONDS,
                    DEFAULT_BACKOFF_SECONDS * (2**attempt),
                )
            )
    raise TushareError(f"Tushare {api_name} request failed")


def to_ts_code(code: str) -> str:
    """把 iFund 六位代码转换为 Tushare 场外基金代码（无映射表时的兜底）。"""
    value = str(code).strip().upper()
    return value if value.endswith((".OF", ".SH", ".SZ")) else f"{value}.OF"


def resolve_ts_code(code: str) -> str:
    """映射表优先解析 ts_code；导入失败时回退 ``to_ts_code``。"""
    value = str(code).strip().upper()
    if value.endswith((".OF", ".SH", ".SZ")):
        return value
    try:
        from app.fund_nav import ts_code_map as _map  # pylint: disable=import-outside-toplevel

        return _map.resolve_ts_code(value)
    except Exception:  # pylint: disable=broad-exception-caught
        return to_ts_code(value)


def to_yyyymmdd(value: str | None) -> str | None:
    """把 YYYY-MM-DD 转为 Tushare 所需的 YYYYMMDD。"""
    if not value:
        return None
    value = str(value).strip()
    return value.replace("-", "")


def fetch_fund_nav(
    code: str,
    start_date: str | None = None,
    end_date: str | None = None,
    *,
    timeout: tuple[int, int] = REQUEST_TIMEOUT,
) -> list[dict]:
    """拉取单只基金完整/区间单位、累计和前复权净值。"""
    params: dict[str, object] = {"ts_code": resolve_ts_code(code)}
    if start_date:
        params["start_date"] = to_yyyymmdd(start_date)
    if end_date:
        params["end_date"] = to_yyyymmdd(end_date)
    return call(
        "fund_nav",
        params,
        "ts_code,nav_date,unit_nav,accum_nav,adj_nav",
        timeout=timeout,
    )


def fetch_fund_div(
    code: str,
    start_date: str | None = None,
    end_date: str | None = None,
    *,
    timeout: tuple[int, int] = REQUEST_TIMEOUT,
) -> list[dict]:
    """拉取单只基金分红事件。"""
    # fund_div 的官方筛选参数是 ann_date/div_date/pay_date，而不是行情接口的
    # start_date/end_date；治理任务拉全史，因此只传 ts_code，避免发送非法参数。
    del start_date, end_date
    return call(
        "fund_div",
        {"ts_code": resolve_ts_code(code)},
        "ts_code,ann_date,ex_date,record_date,pay_date,div_cash",
        timeout=timeout,
    )


def fetch_fund_split(
    code: str,
    start_date: str | None = None,
    end_date: str | None = None,
    *,
    timeout: tuple[int, int] = REQUEST_TIMEOUT,
) -> list[dict]:
    """拉取单只基金拆分事件。"""
    del start_date, end_date
    return call(
        "fund_split",
        {"ts_code": resolve_ts_code(code)},
        "ts_code,ann_date,split_date,split_ratio",
        timeout=timeout,
    )
