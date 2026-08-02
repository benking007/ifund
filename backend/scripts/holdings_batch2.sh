#!/bin/bash
# ifund 持仓补拉串联脚本：债券 / FOF / QDII 三类（货币不跑）
# 按类型逐个拉取，每类型记录 BEFORE/AFTER 与 rc，失败继续下一类型
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(dirname "$SCRIPT_DIR")"
cd "$BACKEND_DIR" || exit 1
PY="$BACKEND_DIR/venv/bin/python3.12"
DB="$BACKEND_DIR/data.db"
LOG_TS=$(date +%Y%m%d_%H%M%S)
BATCH_LOG="$BACKEND_DIR/logs/holdings_batch2_${LOG_TS}.log"
mkdir -p "$BACKEND_DIR/logs"

count_covered() {
  "$PY" -c "import sqlite3;print(sqlite3.connect('$DB').execute('SELECT COUNT(DISTINCT fund_code) FROM fund_holdings').fetchone()[0])"
}

echo "[$(date '+%F %T')] ===== ifund 持仓补拉（债券/FOF/QDII）开始 =====" | tee -a "$BATCH_LOG"

TYPES=(
  # 债券类
  "债券型-长债" "债券型-中短债" "债券型-利率债" "债券型-信用债"
  "债券型-混合一级" "债券型-混合二级" "指数型-固收"
  # FOF
  "FOF-稳健型" "FOF-均衡型" "FOF-进取型"
  # QDII
  "QDII-混合偏股" "QDII-普通股票" "QDII-纯债" "QDII-混合灵活"
  "QDII-混合债" "QDII-商品" "QDII-FOF" "QDII-混合平衡" "QDII-REITs"
)

for ftype in "${TYPES[@]}"; do
  echo "" | tee -a "$BATCH_LOG"
  echo "[$(date '+%F %T')] === 类型: ${ftype} 开始 ===" | tee -a "$BATCH_LOG"
  before=$(count_covered)
  echo "[$(date '+%F %T')] BEFORE=${before}" | tee -a "$BATCH_LOG"
  env IFUND_CLI_CONCURRENCY=2 "$PY" -m cli fetch holdings --types "${ftype}" --json >> "$BATCH_LOG" 2>&1
  rc=$?
  after=$(count_covered)
  echo "[$(date '+%F %T')] 类型: ${ftype} END rc=${rc} AFTER=${after} (+$((after-before)))" | tee -a "$BATCH_LOG"
  if [ "$rc" -ne 0 ]; then
    echo "[$(date '+%F %T')] 类型: ${ftype} 失败 rc=${rc}，继续下一类型" | tee -a "$BATCH_LOG"
  fi
done

echo "" | tee -a "$BATCH_LOG"
echo "[$(date '+%F %T')] ===== 全部类型拉取完成，最终覆盖: $(count_covered) =====" | tee -a "$BATCH_LOG"
