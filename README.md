# Calorie Tracker — Cronometer Automation

Automatically log your company's daily lunch into [Cronometer](https://cronometer.com) from a single command. Paste the meal name, confirm the ingredient breakdown, and the tool handles the rest.

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
  • tries progressively simpler terms if no match
  • falls back to a custom food entry if still not found
  • adds everything to your diary under Lunch
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

# 2. Create your credentials file from the example
cp auth.json.example auth.json
# Edit auth.json and fill in your Cronometer email and password

# 3. Build the Docker image (one-time, takes a few minutes)
docker compose build
```

## Daily usage

Paste the canteen menu item and run:

```bash
./add_meal.sh "Chicken Burger Deluxe - Crispy Chicken Fillet, Ranch Sauce, Romaine Lettuce & Herb Fries"
```

You will see an ingredient breakdown:

```
┌────────────────────────────────────────────────────────────────────────┐
│  Chicken Burger Deluxe with Herb Fries                                 │
├──────────────────────────────────┬───────┬───────┬───────┬───────┬──────┤
│  Ingredient                      │  kcal │  Prot │   Fat │  Carb │    g │
├──────────────────────────────────┼───────┼───────┼───────┼───────┼──────┤
│  Hamburger bun                   │   220 │   7.0 │   3.5 │  41.0 │   80 │
│  Breaded chicken fillet fried    │   295 │  27.0 │  13.0 │  14.0 │  155 │
│  Ranch dressing                  │   130 │   0.5 │  13.0 │   2.5 │   30 │
│  Romaine lettuce                 │     3 │   0.3 │   0.0 │   0.6 │   20 │
│  French fries                    │   430 │   5.0 │  19.0 │  59.0 │  180 │
├──────────────────────────────────┼───────┼───────┼───────┼───────┼──────┤
│  TOTAL                           │  1078 │       │       │       │      │
└──────────────────────────────────┴───────┴───────┴───────┴───────┴──────┘

Log these 5 ingredients to Cronometer for 2026-04-16? [Y/n]:
```

Confirm and the browser automation runs silently in the background.

## Options

```bash
# Log to a specific past or future date
./add_meal.sh --date 2026-04-15 "your meal"

# Only show the nutrition estimate without logging anything
./add_meal.sh --estimate-only "Spring Vegetable Soup with Pearl Barley and Asparagus"
```

## Project structure

```
calorie-tracker/
├── add_meal.sh          # Entry point — run this daily
├── add_meal.py          # Playwright browser automation (runs inside Docker)
├── auth.json            # Your Cronometer credentials (git-ignored)
├── auth.json.example    # Template showing the expected format
├── Dockerfile           # Playwright + Python container image
├── docker-compose.yml   # Container configuration
└── requirements.txt     # Python dependencies (playwright only)
```

## Credentials

Copy `auth.json.example` to `auth.json` and fill in your details. The file is listed in `.gitignore` and will never be committed.

## Troubleshooting

**"Too Many Attempts"** — Cronometer rate-limits login attempts. Wait 5–10 minutes before retrying. After a successful login the session is cached locally, so future runs skip the login step entirely.

**Ingredient not found in Cronometer** — The tool automatically retries with simpler search terms before giving up. If nothing matches, it creates a custom food entry using the estimated nutrition values.

**Something looks wrong** — Debug screenshots are saved to the project folder as `debug_*.png` after every run, showing exactly what the browser was doing at each step.

## Why browser automation instead of an API?

Cronometer does not offer a public write API for individual users. Their internal API uses GWT-RPC (Google Web Toolkit Remote Procedure Call), a proprietary binary protocol tied to their web app build — it is not designed to be called externally and breaks whenever they deploy. Browser automation via Playwright is the only stable approach for automating diary writes.
# Calorie-Tracker-Cronometer-Automation
