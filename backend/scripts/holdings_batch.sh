#!/usr/bin/env bash
set -u

export IFUND_CLI_CONCURRENCY="${IFUND_CLI_CONCURRENCY:-2}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(dirname "$SCRIPT_DIR")"
cd "$BACKEND_DIR" || exit 1

mkdir -p logs
LOG_FILE="logs/holdings_batch_$(date +%Y%m%d_%H%M%S).log"
DB_FILE="$BACKEND_DIR/data.db"

count_holdings() {
  if which sqlite3 >/dev/null 2>&1; then
    sqlite3 "$DB_FILE" 'SELECT COUNT(DISTINCT fund_code) FROM fund_holdings;' 2>/dev/null || echo 0
  else
    python3 -c 'import sqlite3, sys
try:
    with sqlite3.connect(sys.argv[1]) as conn:
        row = conn.execute("SELECT COUNT(DISTINCT fund_code) FROM fund_holdings").fetchone()
    print(row[0] if row else 0)
except Exception:
    print(0)' "$DB_FILE"
  fi
}

{
  echo "[$(date '+%F %T')] holdings batch started"
  echo "Database: $DB_FILE"
  echo "Log: $(pwd)/$LOG_FILE"
} >> "$LOG_FILE"

for ftype in "股票型" "混合型-偏股" "指数型-股票"; do
  before="$(count_holdings)"
  {
    echo
    echo "[$(date '+%F %T')] [START] type=$ftype"
    echo "[$(date '+%F %T')] [BEFORE] fund_holdings distinct fund_code=$before"
  } >> "$LOG_FILE"

  "$BACKEND_DIR/venv/bin/python3.12" -m cli fetch holdings --types "$ftype" --json >> "$LOG_FILE" 2>&1
  rc=$?

  after="$(count_holdings)"
  {
    echo "[$(date '+%F %T')] [END] type=$ftype rc=$rc"
    echo "[$(date '+%F %T')] [AFTER] fund_holdings distinct fund_code=$after"
  } >> "$LOG_FILE"
done

final_count="$(count_holdings)"
{
  echo
  echo "[$(date '+%F %T')] holdings batch finished"
  echo "Final distinct fund_holdings fund_code count: $final_count"
  echo "Log path: $(pwd)/$LOG_FILE"
} >> "$LOG_FILE"

echo "Final distinct fund_holdings fund_code count: $final_count"
echo "Log path: $(pwd)/$LOG_FILE"
