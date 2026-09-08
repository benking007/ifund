#!/usr/bin/env bash
# iFund 事件/复权重跑：探测 Tushare 恢复后自动执行 events → adj_tushare → calc 定向覆盖。
# 探测策略：每 10 分钟一次 fund_div，连续 2 次成功才放行（避免时段性抖动误判）。
set -u
BACKEND_DIR=/root/workspace/ifund/backend
PY="$BACKEND_DIR/venv/bin/python3.12"
CLI="$BACKEND_DIR/ifund_cli.py"
LOG="$BACKEND_DIR/logs/nav_gov_events_adj.log"
PROBE="$BACKEND_DIR/scripts/probe_tushare.py"

exec >> "$LOG" 2>&1
echo "===== events_adj start $(date '+%F %T') pid=$$ ====="

set -a
# shellcheck disable=SC1091
. /etc/ifund-prod.env
set +a
export DB_BACKEND=mysql
export IFUND_TUSHARE_RATE_PER_MIN="${IFUND_TUSHARE_RATE_PER_MIN:-20}"
cd "$BACKEND_DIR" || exit 1

# ---- 探测循环（连续 2 次成功放行；放行后进入批量阶段） ----
streak=0
probes=0
while [ "$streak" -lt 2 ]; do
  probes=$((probes + 1))
  out=$("$PY" "$PROBE" 2>&1)
  rc=$?
  echo "[probe#$probes] $(date '+%F %T') rc=$rc $out"
  if [ "$rc" -eq 0 ]; then
    streak=$((streak + 1))
  else
    streak=0
  fi
  if [ "$streak" -lt 2 ]; then
    sleep 600
  fi
done
echo "[gate] Tushare 连续 2 次探测成功，放行 $(date '+%F %T')"
# tushare_client 还会通过 fin-data 共享状态文件执行跨进程总闸。
export TUSHARE_INTERVAL_MS="${TUSHARE_INTERVAL_MS:-3000}"

run_stage() {
  local name="$1"; shift
  echo "----- [$name] start $(date '+%F %T') -----"
  "$@"
  echo "----- [$name] end rc=$? $(date '+%F %T') -----"
}

# 阶段1：分红拆分事件全量（Tushare，27k 只；内置 400ms 限速 + 指数退避）
run_stage "events" "$PY" "$CLI" nav events --all --concurrency 2

# 阶段2：前复权净值 Tushare 全量（同样限速退避）
run_stage "adj-tushare" "$PY" "$CLI" nav adj --src tushare --all --concurrency 2

# 阶段3：calc 定向覆盖——只重算"有分红拆分事件"的基金（覆盖昨夜错误写的 adj=nav）
CODES=$("$PY" - <<'PYEOF'
import sqlite3
conn = sqlite3.connect("/root/workspace/ifund/backend/data.db", timeout=10)
cur = conn.execute("SELECT DISTINCT fund_code FROM fund_div_split ORDER BY fund_code")
codes = [r[0] for r in cur.fetchall()]
conn.close()
print(",".join(codes))
PYEOF
)
echo "[calc] 有事件基金数量: $(echo "$CODES" | awk -F',' '{print NF}')"
if [ -n "$CODES" ]; then
  run_stage "adj-calc-refresh" "$PY" "$CLI" nav adj --src calc --codes "$CODES" --concurrency 2
else
  echo "[calc] 无事件基金，跳过定向覆盖"
fi

echo "===== events_adj done $(date '+%F %T') ====="
