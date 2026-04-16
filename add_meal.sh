#!/usr/bin/env bash
# add_meal.sh — Add your company lunch to Cronometer.
#
# Runs claude on the HOST (where it's authenticated) for the nutrition estimate,
# then hands off to Docker (Playwright) for the actual Cronometer automation.
#
# USAGE:
#   ./add_meal.sh "Chickeria Burger Deluxe - Hähnchenfilet, Ranch Sauce, Römersalat"
#   ./add_meal.sh --date 2026-04-16 "your meal"
#   ./add_meal.sh --estimate-only "your meal"
#   ./add_meal.sh                              # interactive: paste, Enter twice

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/config.json"

# ── Load credentials (keychain → secret-service → .env → auth.json) ──────────
_load_credentials() {
    # 1. macOS Keychain
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

    # 2. Linux Secret Service (GNOME Keyring / KWallet)
    if command -v secret-tool &>/dev/null; then
        local pass
        pass=$(secret-tool lookup service "cronometer.com" username "$(cat "$SCRIPT_DIR/.crono_user" 2>/dev/null)" 2>/dev/null || true)
        if [[ -n "$pass" ]]; then
            CRONOMETER_USER=$(cat "$SCRIPT_DIR/.crono_user")
            CRONOMETER_PASSWORD="$pass"
            return
        fi
    fi

    # 3. .env file (headless Linux / Raspberry Pi)
    if [[ -f "$SCRIPT_DIR/.env" ]]; then
        # shellcheck disable=SC1091
        source "$SCRIPT_DIR/.env"
        if [[ -n "${CRONOMETER_USER:-}" && -n "${CRONOMETER_PASSWORD:-}" ]]; then
            return
        fi
    fi

    # 4. Legacy auth.json fallback
    if [[ -f "$SCRIPT_DIR/auth.json" ]]; then
        CRONOMETER_USER=$(python3 -c "import json; d=json.load(open('$SCRIPT_DIR/auth.json')); print(d['http-basic']['https://cronometer.com']['username'])")
        CRONOMETER_PASSWORD=$(python3 -c "import json; d=json.load(open('$SCRIPT_DIR/auth.json')); print(d['http-basic']['https://cronometer.com']['password'])")
        return
    fi

    echo "Error: no credentials found. Run ./setup.sh first." >&2
    exit 1
}

_load_credentials
export CRONOMETER_USER
export CRONOMETER_PASSWORD

# ── Load defaults from config.json (if present) ────────────────────────────────
_cfg() {
    # Usage: _cfg <key> <fallback>
    # Reads .defaults.<key> from config.json using python3 (always available in Docker env)
    if [[ -f "$CONFIG_FILE" ]]; then
        python3 -c "
import json, sys
try:
    d = json.load(open('$CONFIG_FILE')).get('defaults', {})
    print(d.get('$1', '$2'))
except Exception:
    print('$2')
"
    else
        echo "$2"
    fi
}

LOG_DATE_OFFSET=$(_cfg date_offset 0)
LOG_DATE=$(date -v+"${LOG_DATE_OFFSET}d" +%Y-%m-%d 2>/dev/null || date -d "+${LOG_DATE_OFFSET} days" +%Y-%m-%d)
ESTIMATE_ONLY=false
DEBUG=false
MEAL=$(_cfg meal "Lunch")
PORTION=$(_cfg portion "normal")

# ── Argument parsing — flags override config defaults ─────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --date)          LOG_DATE="$2"; shift 2 ;;
        --meal)          MEAL="$2"; shift 2 ;;
        --portion)       PORTION="$2"; shift 2 ;;
        --estimate-only) ESTIMATE_ONLY=true; shift ;;
        --debug)         DEBUG=true; shift ;;
        --)              shift; break ;;
        -*)              echo "Unknown flag: $1" >&2; exit 1 ;;
        *)               break ;;
    esac
done

echo "Using defaults → meal: $MEAL | portion: $PORTION | date: $LOG_DATE"

MEAL="${*:-}"

# ── Interactive input if no meal given ────────────────────────────────────────
if [[ -z "$MEAL" ]]; then
    echo "Paste the meal description (press Enter twice when done):"
    lines=()
    while IFS= read -r line; do
        [[ -z "$line" ]] && (( ${#lines[@]} > 0 )) && break
        lines+=("$line")
    done
    MEAL=$(printf '%s\n' "${lines[@]}")
fi

if [[ -z "$MEAL" ]]; then
    echo "Error: no meal description provided." >&2
    exit 1
fi

# ── Estimate nutrition via Claude Code on the host ────────────────────────────
echo ""
echo "Estimating nutrition for: ${MEAL:0:80}..."

TMPJSON=$(mktemp /tmp/meal_nutrition_XXXXXX.json)
trap 'rm -f "$TMPJSON"' EXIT

PORTION_NOTE="Portion size: normal (standard canteen serving)."
case "$PORTION" in
    small)    PORTION_NOTE="Portion size: small (scale gram amounts down ~25% from a standard serving)." ;;
    generous) PORTION_NOTE="Portion size: generous (scale gram amounts up ~25% from a standard serving)." ;;
    large)    PORTION_NOTE="Portion size: large (scale gram amounts up ~50% from a standard serving)." ;;
esac

claude -p "You are a registered dietitian with expertise in German/European cafeteria food.
Break this meal into individual components for Cronometer food diary tracking.
Use English ingredient names that Cronometer's USDA/NCCDB food database would recognise — prefer generic names (e.g. 'hamburger bun white' over brand names).
${PORTION_NOTE}

Meal: ${MEAL}

Reply with ONLY a valid JSON object, no markdown, no explanation:
{\"meal_name\":\"<short name>\",\"ingredients\":[{\"search_name\":\"<English name for Cronometer search, 2-4 words, generic>\",\"amount_g\":<number>,\"calories\":<integer>,\"protein_g\":<number>,\"fat_g\":<number>,\"carbs_g\":<number>,\"fiber_g\":<number>,\"sugar_g\":<number>,\"sodium_mg\":<number>}]}" > "$TMPJSON"

# ── Display ingredient table ───────────────────────────────────────────────────
python3 - "$TMPJSON" <<'PYEOF'
import json, re, sys
with open(sys.argv[1]) as f:
    text = f.read().strip()
if "```" in text:
    text = re.sub(r"```\w*\n?", "", text).strip()
d = json.loads(text)
ings = d["ingredients"]
total = sum(i["calories"] for i in ings)
W = 72
print(f"\n┌{'─'*W}┐")
print(f"│  {d['meal_name'][:W-2]:<{W-2}}│")
print(f"├{'─'*34}┬{'─'*7}┬{'─'*7}┬{'─'*7}┬{'─'*7}┬{'─'*6}┤")
print(f"│  {'Ingredient':<32}│  kcal │  Prot │   Fat │  Carb │    g │")
print(f"├{'─'*34}┼{'─'*7}┼{'─'*7}┼{'─'*7}┼{'─'*7}┼{'─'*6}┤")
for i in ings:
    n = i["search_name"][:31]
    print(f"│  {n:<31} │{i['calories']:>6} │{i['protein_g']:>6.1f} │{i['fat_g']:>6.1f} │{i['carbs_g']:>6.1f} │{i['amount_g']:>5} │")
print(f"├{'─'*34}┼{'─'*7}┼{'─'*7}┼{'─'*7}┼{'─'*7}┼{'─'*6}┤")
print(f"│  {'TOTAL':<31} │{total:>6} │{'':>6} │{'':>6} │{'':>6} │{'':>5} │")
print(f"└{'─'*34}┴{'─'*7}┴{'─'*7}┴{'─'*7}┴{'─'*7}┴{'─'*6}┘")
PYEOF

[[ "$ESTIMATE_ONLY" == "true" ]] && exit 0

# ── Confirm ────────────────────────────────────────────────────────────────────
read -rp $"\nLog this to Cronometer for ${LOG_DATE}? [Y/n]: " CONFIRM
CONFIRM="${CONFIRM:-y}"
[[ "$CONFIRM" =~ ^[Nn] ]] && { echo "Aborted."; exit 0; }

# ── Hand off to Docker for Playwright automation ───────────────────────────────
# -T disables TTY allocation (piping stdin); the container reads the JSON and
# runs Playwright headlessly — no interaction needed at this point.
echo ""
DOCKER_FLAGS="--nutrition-from-stdin --date $LOG_DATE --meal $MEAL"
[[ "$DEBUG"   == "true" ]] && DOCKER_FLAGS="$DOCKER_FLAGS --debug"

docker compose run --rm -T \
    -e CRONOMETER_USER="$CRONOMETER_USER" \
    -e CRONOMETER_PASSWORD="$CRONOMETER_PASSWORD" \
    calorie-tracker $DOCKER_FLAGS < "$TMPJSON"
