#!/usr/bin/env bash
# 等 A 股财务全量回填退出后，再以共享总闸 20/min 续跑 fund_share。
set -u
BACKEND=/root/workspace/ifund/backend
LOG="$BACKEND/logs/fund_share_backfill.log"

exec >> "$LOG" 2>&1
echo "===== fund_share scheduler start $(date '+%F %T') pid=$$ ====="
while pgrep -f 'sync.tushare_financial_backfill.*--mode full' >/dev/null; do
  echo "[wait] financial backfill still running $(date '+%F %T')"
  sleep 60
done

# 净值在工作日 20:00-23:50 每 5 分钟轮询。若财务回填恰在该窗口退出，
# 把 fund_share 推迟到次日 00:30，避免与净值任务争用共享限流器和数据库。
read -r now_hour now_minute now_second < <(/bin/date '+%H %M %S')
now_minutes=$((10#$now_hour * 60 + 10#$now_minute))
if ((now_minutes >= 20 * 60 && now_minutes <= 23 * 60 + 50)); then
  wait_seconds=$(((24 * 60 + 30) * 60 \
    - (10#$now_hour * 3600 + 10#$now_minute * 60 + 10#$now_second)))
  echo "[wait] NAV window active $(date '+%F %T'); delaying fund_share ${wait_seconds}s until 00:30"
  sleep "$wait_seconds"
  echo "[wait] NAV avoidance ended $(date '+%F %T'); starting fund_share"
fi

set -a
# shellcheck disable=SC1091
. /etc/ifund-prod.env
set +a
export DB_BACKEND=mysql
export IFUND_TUSHARE_RATE_PER_MIN="${IFUND_TUSHARE_RATE_PER_MIN:-20}"

cd "$BACKEND" || exit 1
exec ./venv/bin/python3.12 scripts/sync_tushare_fund_share.py \
  --all --resume --start 1990-01-01
