"""网络调用的统一超时与重试判定。"""
from __future__ import annotations

import threading
from collections.abc import Iterator
from urllib.error import URLError

import requests


HTTP_TIMEOUT = (5, 15)
MAX_NETWORK_ATTEMPTS = 3


class TimeoutRequestsProxy:
    """为第三方模块自己的 ``requests`` 引用注入默认超时。"""

    _ifund_timeout_proxy = True

    def __init__(self, requests_module, timeout=HTTP_TIMEOUT) -> None:
        self._requests_module = requests_module
        self._timeout = timeout
        self._local = threading.local()

    def _session(self):
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._requests_module.Session()
            self._local.session = session
        return session

    def _call(self, method: str, *args, **kwargs):
        kwargs.setdefault("timeout", self._timeout)
        return getattr(self._session(), method)(*args, **kwargs)

    def request(self, *args, **kwargs):
        """代理 ``requests.request``。"""
        return self._call("request", *args, **kwargs)

    def get(self, *args, **kwargs):
        """代理 ``requests.get``。"""
        return self._call("get", *args, **kwargs)

    def post(self, *args, **kwargs):
        """代理 ``requests.post``。"""
        return self._call("post", *args, **kwargs)

    def put(self, *args, **kwargs):
        """代理 ``requests.put``。"""
        return self._call("put", *args, **kwargs)

    def patch(self, *args, **kwargs):
        """代理 ``requests.patch``。"""
        return self._call("patch", *args, **kwargs)

    def delete(self, *args, **kwargs):
        """代理 ``requests.delete``。"""
        return self._call("delete", *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._requests_module, name)


def install_module_timeout(module, timeout=HTTP_TIMEOUT) -> None:
    """幂等替换第三方模块的 requests 引用，不修改全局 requests。"""
    requests_module = getattr(module, "requests", None)
    if requests_module is not None and getattr(requests_module, "_ifund_timeout_proxy", False) is not True:
        module.requests = TimeoutRequestsProxy(requests_module, timeout=timeout)


class NetworkRetryExhausted(RuntimeError):
    """网络错误已完成有限重试，阻止外层 worker 再次成倍重试。"""

    def __init__(self, label: str, attempts: int) -> None:
        super().__init__(f"{label} 网络请求已尝试 {attempts} 次")
        self.attempts = attempts


def _exception_chain(exc: BaseException) -> Iterator[BaseException]:
    """遍历包装异常，供 Tushare 等客户端保留底层网络错误语义。"""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def is_retryable_network_error(exc: BaseException) -> bool:
    """仅把 timeout、连接失败和 HTTP 5xx 视为可重试错误。"""
    if isinstance(exc, NetworkRetryExhausted):
        return False
    for current in _exception_chain(exc):
        if isinstance(current, (requests.Timeout, requests.ConnectionError, TimeoutError, ConnectionError)):
            return True
        if isinstance(current, requests.HTTPError):
            status = getattr(current.response, "status_code", None)
            if status is not None and 500 <= int(status) < 600:
                return True
        if isinstance(current, URLError):
            reason = current.reason
            if isinstance(reason, (TimeoutError, ConnectionError, OSError)):
                return True
    return False
