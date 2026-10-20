"""净值修复队列失败分类：可重试、可归档，以及 last_error 文本校正。"""

from __future__ import annotations

import re


RETRY_NAV = "retry_nav"
RETRY_EVENTS = "retry_events"
RETRY_ADJ = "retry_adj"
ARCHIVE_ADJ_EMPTY = "archive_adj_empty"
REVIEW = "review"

RETRY_BUCKETS = (RETRY_NAV, RETRY_EVENTS, RETRY_ADJ)
ACTION_ORDER = (RETRY_NAV, RETRY_EVENTS, RETRY_ADJ, ARCHIVE_ADJ_EMPTY, REVIEW)
FALLBACK_FAILURE = "治理任务失败"
ARCHIVED_STATUS = "archived"
STATUS_CHECK_SQL = (
    "status IN ('pending','running','done','failed','archived')"
)
_BARE_CODE = re.compile(r"^\d{6}$")


def _is_code_list(text: str) -> bool:
    """last_error 被写成基金代码（或分号拼接的代码）时返回 True。"""
    parts = [part.strip() for part in text.split(";") if part.strip()]
    return bool(parts) and all(_BARE_CODE.fullmatch(part) for part in parts)


def safe_error_text(error: object, fallback: str = FALLBACK_FAILURE) -> str:
    """把异常文本收成可入库的 last_error，纯基金代码改写成明确占位。"""
    text = str(error or "").strip()
    if not text or _is_code_list(text):
        return fallback
    return text[:2000]


def failure_message(result: dict) -> str:
    """从复权拉取结果取出真实错误，拒绝把基金代码写进 last_error。"""
    errors = [
        str(item).strip()
        for item in (result.get("errors") or [])
        if str(item).strip()
    ]
    return safe_error_text(";".join(errors))


def classify_failure(last_error: str | None, task_kind: str | None = None) -> str:
    """按现有错误文本归入重试、归档或人工复核。"""
    text = str(last_error or "").strip()
    if "请指定正确的接口名" in text:
        return RETRY_EVENTS
    if "服务异常" in text or "请稍后再试" in text:
        return RETRY_NAV
    if "Data_netWorthTrend is not defined" in text:
        return RETRY_NAV
    if "adj_nav 返回空" in text or "无有效行" in text:
        return ARCHIVE_ADJ_EMPTY
    if _is_code_list(text):
        return RETRY_NAV if task_kind == "nav" else RETRY_ADJ
    return REVIEW


def group_failures(rows: list[dict]) -> dict[str, list[dict]]:
    """按动作分桶，保留输入顺序。"""
    grouped = {key: [] for key in ACTION_ORDER}
    for row in rows:
        key = classify_failure(row.get("last_error"), row.get("task_kind"))
        grouped.setdefault(key, []).append(row)
    return grouped


def select_batch(grouped: dict[str, list[dict]], bucket: str, limit: int) -> list[dict]:
    """选出本批要处理的行。limit 作用于每个桶，而不是所有桶的总和。"""
    if bucket == "all_retryable":
        keys = RETRY_BUCKETS
    elif bucket in ACTION_ORDER and bucket != REVIEW:
        keys = (bucket,)
    else:
        raise ValueError(f"未知或不允许执行的 bucket: {bucket}")
    chosen = []
    size = max(0, int(limit))
    for key in keys:
        for row in grouped.get(key, [])[:size]:
            item = dict(row)
            item["action"] = key
            chosen.append(item)
    return chosen


def plan_status_check_ddl(checks: list[dict]) -> list[str]:
    """根据现有 CHECK 子句生成允许 archived 的 ALTER。已包含则返回空列表。"""
    drops: list[str] = []
    allows_archived = False
    for item in checks:
        clause = str(item.get("clause") or "")
        if "status" not in clause.lower():
            continue
        if "archived" in clause.lower():
            allows_archived = True
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            raise ValueError("status CHECK 缺少约束名，无法生成 DROP CHECK")
        drops.append(f"ALTER TABLE nav_repair_queue DROP CHECK `{name}`")
    if allows_archived and not drops:
        return []
    statements = list(drops)
    if not allows_archived:
        statements.append(
            "ALTER TABLE nav_repair_queue ADD CONSTRAINT chk_nav_repair_queue_status "
            f"CHECK ({STATUS_CHECK_SQL})"
        )
    return statements
