# Calorie Tracker — Cronometer Automation

Automatically log your company's daily lunch into [Cronometer](https://cronometer.com) from a single command. Paste the meal name, confirm the ingredient breakdown, and the tool handles the rest.

---

## ⚠️ Important disclaimer

**This tool uses AI to estimate nutritional values. AI estimates are never exact.**

Calorie and macro values are approximations based on typical portion sizes and standard ingredient compositions. The actual nutritional content of your meal depends on factors this tool cannot know: the exact recipe used by your canteen, cooking methods, precise portion weights, ingredient substitutions, and natural variation in food composition.

**This tool is a convenience aid, not a medical or dietary instrument.** It is intended to make the habit of food logging easier — not to replace careful nutritional tracking. If you are managing a medical condition, following a strict diet, or making health decisions based on your calorie data, always verify entries against the actual nutritional information provided by your canteen or use a kitchen scale to weigh portions.

Use the estimates as a reasonable starting point. Check what you are eating against what gets logged. Adjust manually when something looks off.

---

## The problem

Many companies publish a weekly meal plan for their canteen. Every day you'd have to:

1. Read the menu
2. Estimate the calories and macros for each component
3. Search every ingredient individually in Cronometer
4. Add them one by one to your diary

This tool eliminates all of that manual work.

## How it works

```
You paste the meal description
        │
        ▼
Claude Code estimates ingredients + nutrition
        │
        ▼
You review the breakdown and confirm
        │
        ▼
Docker runs a headless Chromium browser
        │
        ▼
Playwright automates the Cronometer UI:
  • searches each ingredient in the food database
  • tries progressively simpler / alternative terms if no match
  • falls back to a custom food entry only as a last resort
  • adds everything to your diary under the configured meal section
```

**No Anthropic API key needed.** The tool calls the `claude` CLI you're already authenticated with.

## Technology

| Component | Purpose |
|-----------|---------|
| **Claude Code CLI** | Breaks the meal into ingredients and estimates nutrition per component. Runs on your host machine using your existing login — no API key required. |
| **Playwright** | A browser automation library that controls a real Chromium instance. Used because Cronometer has no public API — all diary writes happen through the web UI. |
| **Docker** | Packages Playwright and its Chromium dependency into a self-contained container so nothing needs to be installed on your machine beyond Docker itself. |

## Requirements

- [Docker Desktop](https://www.docker.com/products/docker-desktop/)
- [Claude Code CLI](https://claude.ai/code) — installed and logged in
- A Cronometer account

## Setup

```bash
# 1. Clone the repository
git clone https://github.com/Asian-Dave/Calorie-Tracker-Cronometer-Automation
cd calorie-tracker

# 2. Store your Cronometer credentials securely
./setup.sh
# Stores credentials in the most secure location available for your platform:
#   macOS              → macOS Keychain
#   Linux (desktop)    → GNOME Keyring / KWallet via secret-tool
#   Linux (headless)   → .env file with chmod 600 (Raspberry Pi etc.)
# Credentials are never stored in the Docker image or committed to the repo.

# 3. Create your config file from the example
cp config.json.example config.json
# Edit config.json to set your personal defaults (meal section, portion size, etc.)

# 4. Build the Docker image (one-time, takes a few minutes)
docker compose build
```

> **Note:** `auth.json` is still supported as a fallback if you prefer it, but `./setup.sh` is the recommended approach.

## On credentials & security

`setup.sh` uses the most secure storage available on your platform. Credentials are read from the keychain at runtime by `add_meal.sh` and passed into the Docker container as ephemeral environment variables — they are never written to disk inside the container, never baked into the image, and never committed to the repo.

On a **Raspberry Pi or any headless Linux** machine with no keychain daemon running, `setup.sh` falls back to a `.env` file with `chmod 600` permissions, readable only by your user account.

## Daily usage

Paste the canteen menu item and run:

```bash
./add_meal.sh "Chicken Burger Deluxe - Crispy Chicken Fillet, Ranch Sauce, Romaine Lettuce & Herb Fries"
```

The script prints your active defaults from `config.json`, then shows an ingredient breakdown:

```
Using defaults → meal: Lunch | portion: generous | date: 2026-04-16

┌────────────────────────────────────────────────────────────────────────┐
│  Chicken Burger Deluxe with Herb Fries                                 │
├──────────────────────────────────┬───────┬───────┬───────┬───────┬──────┤
│  Ingredient                      │  kcal │  Prot │   Fat │  Carb │    g │
├──────────────────────────────────┼───────┼───────┼───────┼───────┼──────┤
│  Hamburger bun white             │   220 │   7.0 │   3.5 │  41.0 │   80 │
│  Breaded chicken fillet fried    │   295 │  27.0 │  13.0 │  14.0 │  155 │
│  Ranch dressing                  │   130 │   0.5 │  13.0 │   2.5 │   30 │
│  Romaine lettuce                 │     3 │   0.3 │   0.0 │   0.6 │   20 │
│  French fries                    │   430 │   5.0 │  19.0 │  59.0 │  180 │
├──────────────────────────────────┼───────┼───────┼───────┼───────┼──────┤
│  TOTAL                           │  1078 │       │       │       │      │
└──────────────────────────────────┴───────┴───────┴───────┴───────┴──────┘

Log these 5 ingredients to Cronometer for 2026-04-16? [Y/n]:
```

Confirm, and the browser automation runs silently in the background. Once all ingredients are logged, the tool prints a comparison of the AI-estimated calories against the values Cronometer actually recorded:

```
┌────────────────────────────────────────────────────────────────────────┐
│  Chicken Burger Deluxe with Herb Fries  —  Cronometer summary          │
├──────────────────────────────────┬───────────┬───────────┬─────────────┤
│  Ingredient                      │  AI kcal  │  CRN kcal │    diff     │
├──────────────────────────────────┼───────────┼───────────┼─────────────┤
│  Hamburger bun white             │       220 │       214 │      -6     │
│  Breaded chicken fillet fried    │       295 │       301 │      +6     │
│  Ranch dressing                  │       130 │       118 │     -12     │
│  Romaine lettuce                 │         3 │         3 │       0     │
│  French fries                    │       430 │       441 │     +11     │
├──────────────────────────────────┼───────────┼───────────┼─────────────┤
│  TOTAL                           │      1078 │      1077 │      -1     │
└──────────────────────────────────┴───────────┴───────────┴─────────────┘

Adjust? Enter target kcal or press Enter to finish [1077 kcal logged]:
```

You can then enter a target calorie count. Claude will rescale gram amounts to hit that target, show a preview, and ask for confirmation before clearing and re-adding only the items this session logged (pre-existing entries in the same section are left untouched). The prompt loops until you press Enter without a number.

## Configuration

Edit `config.json` to set your personal defaults. These apply whenever the corresponding flag is not explicitly passed.

```json
{
    "defaults": {
        "meal":        "Lunch",
        "portion":     "generous",
        "date_offset": 0
    }
}
```

| Key | Options | Description |
|-----|---------|-------------|
| `meal` | `Breakfast` `Lunch` `Dinner` `Snacks` | Which diary section to log into |
| `portion` | `small` `normal` `generous` `large` | Scales gram amounts (0.75× / 1.0× / 1.25× / 1.5×) |
| `date_offset` | integer | Days relative to today — set to `-1` to always log yesterday |

## Options

All flags override the corresponding `config.json` default.

```bash
# Use a specific portion size
./add_meal.sh --portion small "your meal"
./add_meal.sh --portion large "your meal"
# options: small | normal | generous | large

# Log to a different meal section
./add_meal.sh --meal Dinner "your meal"
# options: Breakfast | Lunch | Dinner | Snacks

# Log to a specific date
./add_meal.sh --date 2026-04-15 "your meal"

# Only show the nutrition estimate without logging anything
./add_meal.sh --estimate-only "Spring Vegetable Soup with Pearl Barley and Asparagus"

# Save debug screenshots at each step (useful when something goes wrong)
./add_meal.sh --debug "your meal"
```

### Clearing diary entries

`clear_diary.sh` removes entries from a diary section without adding anything new. Useful for correcting mistakes manually.

```bash
# Clear all Lunch entries for today
./clear_diary.sh

# Clear a specific section or date
./clear_diary.sh --section Dinner
./clear_diary.sh --date 2026-04-20 --section Lunch

# Clear only a slice of entries (skip the first N, then delete at most M)
./clear_diary.sh --skip-first 2 --delete-count 3
```

The `--skip-first` / `--delete-count` flags are also what the adjustment loop uses internally to replace only the items it added, leaving any pre-existing entries in the section untouched.

## Project structure

```
calorie-tracker/
├── add_meal.sh          # Entry point — run this daily
├── add_meal.py          # Playwright browser automation (runs inside Docker)
├── clear_diary.sh       # Utility to remove entries from a diary section
├── setup.sh             # One-time credential setup (keychain / .env)
├── config.json          # Your personal defaults (git-ignored)
├── config.json.example  # Template showing the expected format
├── auth.json            # Legacy credentials fallback (git-ignored)
├── auth.json.example    # Template showing the expected format
├── Dockerfile           # Playwright + Python container image
├── docker-compose.yml   # Container configuration
└── requirements.txt     # Python dependencies (playwright only)
```

## Credentials & config

`config.json` and `auth.json` are listed in `.gitignore` and will never be committed. The preferred credential storage is via `./setup.sh` which uses your OS keychain. The `.env` and `.crono_user` files created on headless Linux are also git-ignored.

## Troubleshooting

**"Too Many Attempts"** — Cronometer rate-limits login attempts. Wait 5–10 minutes before retrying. After a successful login the session is cached in `.session.json`, so future runs skip the login step entirely.

**Ingredient not found in Cronometer** — The tool automatically retries with progressively simpler terms, and asks Claude for USDA-friendly alternative names before giving up. Only if nothing matches at all does it fall back to creating a custom food entry.

**Something looks wrong** — Run with `--debug` and the tool saves a screenshot at every step of the browser flow to `debug_*.png`. These are automatically cleaned up after a successful run.

## Why browser automation instead of an API?

Cronometer does not offer a public write API for individual users. Their internal API uses GWT-RPC (Google Web Toolkit Remote Procedure Call), a proprietary binary protocol tied to their web app build — it is not designed to be called externally and breaks whenever they deploy. Browser automation via Playwright is the only stable approach for automating diary writes.
