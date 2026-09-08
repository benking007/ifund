#!/usr/bin/env bash
# iFund 净值数据治理——全量回补（bgjob 后台执行）
# 约束：20:00–23:00 禁跑批量；本脚本 23:00 前启动会自动等待。
set -u
BACKEND_DIR=/root/workspace/ifund/backend
PY="$BACKEND_DIR/venv/bin/python3.12"
CLI="$BACKEND_DIR/ifund_cli.py"
LOG="$BACKEND_DIR/logs/nav_gov_full.log"
GAP_FILE="$BACKEND_DIR/logs/nav_gap_codes.txt"

exec >> "$LOG" 2>&1
echo "===== nav_gov_full start $(date '+%F %T') pid=$$ ====="

# 0. 避峰 gate：仅工作日 20:00–23:00 等待；周末无夜间净值任务，或 FORCE_START=1 时直接放行
DOW=$(date +%u)  # 1=周一 ... 7=周日
now_hm=$(date +%H%M)
if [ "${FORCE_START:-0}" = "1" ]; then
  echo "[gate] FORCE_START=1，直接放行（$(date '+%F %T')，周$DOW）"
elif [ "$DOW" -ge 6 ] || [ "$now_hm" -ge 2300 ] || [ "$now_hm" -lt 2000 ]; then
  echo "[gate] 周$DOW $(date +%T)，非工作日禁跑窗口，直接放行"
else
  echo "[gate] 工作日 $(date +%T) 处于 20:00–23:00 禁跑窗口，等待至 23:05"
  target=$(date -d "today 23:05" +%s)
  now=$(date +%s)
  sleep $(( target - now ))
fi
echo "[gate] 放行时间 $(date '+%F %T')"

# 1. 生产库环境；Tushare token 由客户端从 fin-data 服务配置安全读取。
set -a
# shellcheck disable=SC1091
. /etc/ifund-prod.env
set +a
export DB_BACKEND=mysql
export IFUND_TUSHARE_RATE_PER_MIN="${IFUND_TUSHARE_RATE_PER_MIN:-20}"

cd "$BACKEND_DIR" || exit 1

CODES=$(tr -d '[:space:]' < "$GAP_FILE" 2>/dev/null || echo "")
echo "[stage1] nav 缺口回补 codes 数量: $(echo "$CODES" | awk -F',' '{print NF}')"

run_stage() {
  local name="$1"; shift
  echo "----- [$name] start $(date '+%F %T') -----"
  "$@"
  local rc=$?
  echo "----- [$name] end rc=$rc $(date '+%F %T') -----"
  return 0
}

# 阶段1：单位净值缺口回补（722 只，约 20–40 分钟）
run_stage "stage1-nav-backfill" "$PY" "$CLI" nav backfill \
  --codes "$CODES" --concurrency 8

# 阶段2：分红拆分事件全量采集（Tushare，27k 只，约 3–5 小时）
run_stage "stage2-events" "$PY" "$CLI" nav events --all --concurrency 4

# 阶段3：前复权净值全量（Tushare adj_nav，约 3–5 小时）
run_stage "stage3-adj-tushare" "$PY" "$CLI" nav adj --src tushare --all --concurrency 4

# 阶段4：自算兜底（只补 adj_nav 仍为 NULL 的行，本地计算，约 2–4 小时）
run_stage "stage4-adj-calc" "$PY" "$CLI" nav adj --src calc --only-null --all --concurrency 4

# 阶段5：基金详情过期重拉（自检发现 8/1 后停更、26,038 行全过期；雪球源，低并发礼貌限速，约 12–24 小时）
run_stage "stage5-detail-refresh" env IFUND_CLI_CONCURRENCY=2 "$PY" "$CLI" fetch detail

echo "===== nav_gov_full done $(date '+%F %T') ====="
