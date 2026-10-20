#!/usr/bin/env bash
# 每日增量同步 Tushare fund_div → fund_div_split。
# 从 fund_sync_state(task_kind=div_ann_date) 水位的次日补到昨天。
# 没有水位时退出非 0，避免从 1998 全量回填。DB_BACKEND 由 /etc/ifund-prod.env 提供，缺省 mysql。
set -u
BACKEND=/root/workspace/ifund/backend
LOG="$BACKEND/logs/fund_div_incremental.log"
LOCK="$BACKEND/logs/fund_div_incremental.lock"

mkdir -p "$BACKEND/logs"
exec >> "$LOG" 2>&1
echo "===== fund_div incremental start $(date '+%F %T') pid=$$ ====="

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
export DB_BACKEND="${DB_BACKEND:-mysql}"
export TUSHARE_PREFER_FIN_DATA="${TUSHARE_PREFER_FIN_DATA:-1}"
export TUSHARE_INTERVAL_MS="${TUSHARE_INTERVAL_MS:-3000}"

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
/usr/bin/timeout -s TERM -k 30s 30m "$PY" scripts/sync_fund_div_ann_date.py \
  --require-watermark --interval-ms "${TUSHARE_INTERVAL_MS}"
rc=$?
set -e
if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
  echo "ERROR: fund_div incremental timed out rc=$rc"
fi
echo "===== fund_div incremental end rc=$rc $(date '+%F %T') ====="
exit "$rc"
