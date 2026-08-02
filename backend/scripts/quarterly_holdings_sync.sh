#!/usr/bin/env bash
# 季度披露窗口内自动增量同步基金持仓。
#
# 当前使用一次 --incremental 扫描目标集合；如果未来目标超过 5,000 只，
# 可在这里复用 --types 分成三批串行执行。本次保持单次执行，减少调度复杂度。
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$BACKEND_DIR/logs"
PYTHON="$BACKEND_DIR/venv/bin/python3"

mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/quarterly_sync_$(date +%Y%m%d).log"

# 把脚本自身、CLI 输出和统计信息统一追加到当天日志，便于 cron 无交互运行。
exec >> "$LOG_FILE" 2>&1

log() {
  printf '[%s] %s\n' "$(date '+%F %T')" "$*"
}

month="$(date +%m)"
day="$(date +%d)"
month=$((10#$month))
day=$((10#$day))
force_run="${IFUND_SYNC_FORCE:-0}"
dry_run="${IFUND_SYNC_DRY_RUN:-0}"
concurrency="${IFUND_CLI_CONCURRENCY:-2}"

in_window=0
case "$month" in
  1|4|7|10)
    if ((day >= 20 && day <= 31)); then
      in_window=1
    fi
    ;;
esac

log "quarterly holdings sync started: date=$(date +%F) month=$month day=$day"
log "window check: in_window=$in_window force=$force_run dry_run=$dry_run"

if ((in_window == 0)) && [[ "$force_run" != "1" ]]; then
  log "outside quarterly disclosure window; exiting without fetch"
  exit 0
fi

if [[ "$force_run" == "1" ]] && ((in_window == 0)); then
  log "forced run enabled; ignoring quarterly disclosure window"
fi

if [[ ! -x "$PYTHON" ]]; then
  log "ERROR: Python executable not found or not executable: $PYTHON"
  exit 1
fi

export IFUND_CLI_CONCURRENCY="$concurrency"
COMMAND_DISPLAY="IFUND_CLI_CONCURRENCY=$IFUND_CLI_CONCURRENCY $PYTHON -m cli fetch holdings --incremental --json"
log "command: $COMMAND_DISPLAY"

if [[ "$dry_run" == "1" ]]; then
  log "dry-run enabled; command was not executed"
  log "statistics: not available (dry-run)"
  log "quarterly holdings sync finished: dry-run rc=0"
  exit 0
fi

OUTPUT_FILE="$(mktemp "$LOG_DIR/.quarterly_sync_output.XXXXXX")"
cleanup() {
  rm -f "$OUTPUT_FILE"
}
trap cleanup EXIT

SECONDS=0
"$PYTHON" -m cli fetch holdings --incremental --json > "$OUTPUT_FILE" 2>&1
cli_rc=$?
cat "$OUTPUT_FILE"

# CLI 的 --json 输出包含 total/success/skip/fail；从最后一个合法 JSON 行提取摘要。
stats="$("$PYTHON" - "$OUTPUT_FILE" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as output_file:
    lines = output_file.read().splitlines()

for line in reversed(lines):
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        continue
    if isinstance(payload, dict) and {"total", "success", "skip", "fail"} <= payload.keys():
        print(
            "total={total} new={success} skip={skip} fail={fail}".format(
                total=payload["total"],
                success=payload["success"],
                skip=payload["skip"],
                fail=payload["fail"],
            )
        )
        break
else:
    print("total=unknown new=unknown skip=unknown fail=unknown")
PY
)"
log "statistics: $stats"

if ((cli_rc == 0)); then
  log "quarterly holdings sync finished: rc=0 elapsed=${SECONDS}s"
else
  log "quarterly holdings sync finished: rc=$cli_rc elapsed=${SECONDS}s"
fi

exit "$cli_rc"
