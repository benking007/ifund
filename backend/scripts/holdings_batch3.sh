#!/bin/bash
# ifund 持仓补拉（第三轮）：除货币型外的全部类型，用于补齐此前批次的残余缺口
# 遍历全部非货币类型，cli 自带去重（已存在跳过），单类型失败继续下一类型
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(dirname "$SCRIPT_DIR")"
cd "$BACKEND_DIR" || exit 1
PY="$BACKEND_DIR/venv/bin/python3.12"
DB="$BACKEND_DIR/data.db"
LOG_TS=$(date +%Y%m%d_%H%M%S)
BATCH_LOG="$BACKEND_DIR/logs/holdings_batch3_${LOG_TS}.log"
mkdir -p "$BACKEND_DIR/logs"

count_covered() {
  "$PY" -c "import sqlite3;print(sqlite3.connect('$DB').execute('SELECT COUNT(DISTINCT fund_code) FROM fund_holdings').fetchone()[0])"
}

echo "[$(date '+%F %T')] ===== ifund 持仓补拉（第三轮：非货币全类型）开始 =====" | tee -a "$BATCH_LOG"

# 从 funds 表动态获取除货币型外的所有类型（按未覆盖数量排序）
export IFUND_DB_PATH="$DB"
TYPES=$("$PY" - <<'EOF'
import os, sqlite3
db = sqlite3.connect(os.environ["IFUND_DB_PATH"])
rows = db.execute("""
  SELECT DISTINCT type FROM funds
  WHERE type NOT LIKE '货币型%' AND type != ''
  ORDER BY type
""").fetchall()
print("\n".join(r[0] for r in rows))
EOF
)

for ftype in $TYPES; do
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
