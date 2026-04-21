# clear_diary.sh — Remove all food entries from a diary section (default: Lunch).
#
# USAGE:
#   ./clear_diary.sh                        # clears Lunch for today
#   ./clear_diary.sh --date 2026-04-17      # clears Lunch for a specific date
#   ./clear_diary.sh --section Dinner       # clears a different section
#   ./clear_diary.sh --date 2026-04-17 --section Lunch

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Load credentials (same logic as add_meal.sh) ──────────────────────────────
_load_credentials() {
    if [[ "$(uname -s)" == "Darwin" ]] && command -v security &>/dev/null; then
        local pass
        pass=$(security find-internet-password -s "cronometer.com" -w 2>/dev/null || true)
        if [[ -n "$pass" ]]; then
            CRONOMETER_USER=$(security find-internet-password -s "cronometer.com" -g 2>&1 | \
                grep '"acct"' | sed 's/.*<blob>="//;s/"$//')
            CRONOMETER_PASSWORD="$pass"
            return
        fi
    fi
    if [[ -f "$SCRIPT_DIR/.env" ]]; then
        source "$SCRIPT_DIR/.env"
        [[ -n "${CRONOMETER_USER:-}" && -n "${CRONOMETER_PASSWORD:-}" ]] && return
    fi
    if [[ -f "$SCRIPT_DIR/auth.json" ]]; then
        CRONOMETER_USER=$(python3 -c "import json; d=json.load(open('$SCRIPT_DIR/auth.json')); print(d['http-basic']['https://cronometer.com']['username'])")
        CRONOMETER_PASSWORD=$(python3 -c "import json; d=json.load(open('$SCRIPT_DIR/auth.json')); print(d['http-basic']['https://cronometer.com']['password'])")
        return
    fi
    echo "Error: no credentials found. Run ./setup.sh first." >&2
    exit 1
}

_load_credentials
export CRONOMETER_USER CRONOMETER_PASSWORD

LOG_DATE=$(date +%Y-%m-%d)
SECTION="Lunch"
SKIP_FIRST=""
DELETE_COUNT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --date)         LOG_DATE="$2";    shift 2 ;;
        --section)      SECTION="$2";     shift 2 ;;
        --skip-first)   SKIP_FIRST="$2";  shift 2 ;;
        --delete-count) DELETE_COUNT="$2"; shift 2 ;;
        *)              echo "Unknown flag: $1" >&2; exit 1 ;;
    esac
done

echo "Clearing $SECTION entries for $LOG_DATE..."

EXTRA_FLAGS=""
[[ -n "$SKIP_FIRST"   ]] && EXTRA_FLAGS="$EXTRA_FLAGS --skip-first $SKIP_FIRST"
[[ -n "$DELETE_COUNT" ]] && EXTRA_FLAGS="$EXTRA_FLAGS --delete-count $DELETE_COUNT"

docker compose run --rm -T \
    -e CRONOMETER_USER="$CRONOMETER_USER" \
    -e CRONOMETER_PASSWORD="$CRONOMETER_PASSWORD" \
    calorie-tracker --clear-section "$SECTION" --date "$LOG_DATE" --debug $EXTRA_FLAGS
