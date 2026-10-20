#!/usr/bin/env bash
# 按交易日增量同步 Tushare fund_share。仅 MySQL。
# 水位在 fund_sync_state(task_kind=fund_share_daily)；没有水位时从 MAX(fund_share.trade_date) 的次日开始。
# 最近 5 个自然日如果返回 0 行，不推进水位，下次继续重试。
# 表和水位都空时脚本失败，不会从 1990 全量重拉。
set -u
BACKEND=/root/workspace/ifund/backend
LOG="$BACKEND/logs/fund_share_incremental.log"
LOCK="$BACKEND/logs/fund_share_incremental.lock"

mkdir -p "$BACKEND/logs"
exec >> "$LOG" 2>&1
echo "===== fund_share incremental start $(date '+%F %T') pid=$$ ====="

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "already running $(date '+%F %T')"
  exit 0
fi

if [ -f /etc/ifund-prod.env ]; then
  set -a
  # shellcheck disable=SC1091
  . /etc/ifund-prod.env
  set +a
fi
export DB_BACKEND=mysql

cd "$BACKEND" || { echo "ERROR: cannot cd to $BACKEND"; exit 1; }
if [ -x "$BACKEND/venv/bin/python3.12" ]; then
  PY="$BACKEND/venv/bin/python3.12"
elif [ -x "$BACKEND/venv/bin/python3" ]; then
  PY="$BACKEND/venv/bin/python3"
else
  echo "ERROR: venv python not executable under $BACKEND/venv/bin"
  exit 1
fi

set +e
/usr/bin/timeout -s TERM -k 30s 3h "$PY" scripts/sync_tushare_fund_share.py --incremental
rc=$?
set -e
if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
  echo "ERROR: fund_share incremental timed out rc=$rc"
fi
echo "===== fund_share incremental end rc=$rc $(date '+%F %T') ====="
exit "$rc"
