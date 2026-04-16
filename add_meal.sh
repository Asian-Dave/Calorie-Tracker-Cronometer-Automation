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

# ── Argument parsing ───────────────────────────────────────────────────────────
LOG_DATE=$(date +%Y-%m-%d)
ESTIMATE_ONLY=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --date)          LOG_DATE="$2"; shift 2 ;;
        --estimate-only) ESTIMATE_ONLY=true; shift ;;
        --)              shift; break ;;
        -*)              echo "Unknown flag: $1" >&2; exit 1 ;;
        *)               break ;;
    esac
done

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

claude -p "You are a registered dietitian with expertise in German/European cafeteria food.
Break this meal into individual components for Cronometer food diary tracking.
Use English ingredient names that Cronometer's food database would recognise.

Meal: ${MEAL}

Reply with ONLY a valid JSON object, no markdown, no explanation:
{\"meal_name\":\"<short name>\",\"ingredients\":[{\"search_name\":\"<English name for Cronometer search>\",\"amount_g\":<number>,\"calories\":<integer>,\"protein_g\":<number>,\"fat_g\":<number>,\"carbs_g\":<number>,\"fiber_g\":<number>,\"sugar_g\":<number>,\"sodium_mg\":<number>}]}" > "$TMPJSON"

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
docker compose run --rm -T calorie-tracker \
    --nutrition-from-stdin \
    --date "$LOG_DATE" \
    < "$TMPJSON"
