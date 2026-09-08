#!/usr/bin/env bash
# 基金分红 ann_date 全量后台回填（低速率，与财务回填共存）
set -u
BACKEND=/root/workspace/ifund/backend
PY="$BACKEND/venv/bin/python3.12"
LOG="$BACKEND/logs/fund_div_ann_date_backfill.log"
LOCK="$BACKEND/logs/fund_div_ann_date_backfill.lock"

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "already running" >&2
  exit 0
fi

exec >> "$LOG" 2>&1
echo "===== fund_div ann_date backfill start $(date '+%F %T') pid=$$ ====="

set -a
# shellcheck disable=SC1091
. /etc/ifund-prod.env
set +a
export TUSHARE_PREFER_FIN_DATA=1
export TUSHARE_INTERVAL_MS="${TUSHARE_INTERVAL_MS:-3000}"
export DB_BACKEND=mysql

cd "$BACKEND" || exit 1
"$PY" scripts/sync_fund_div_ann_date.py --interval-ms "${TUSHARE_INTERVAL_MS}"
rc=$?
echo "===== fund_div ann_date backfill end rc=$rc $(date '+%F %T') ====="
exit "$rc"
