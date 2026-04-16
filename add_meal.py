#!/usr/bin/env python3
"""
add_meal.py — Automatically add your company's daily lunch to Cronometer.

Run via add_meal.sh (handles Claude estimation + confirmation on the host).
The container only does the Playwright automation.

SETUP (first time):
    pip install -r requirements.txt
    playwright install chromium
    # No API key needed — uses the Claude Code CLI you already have installed
"""

import sys
import json
import re
import asyncio
import argparse
from datetime import date
from pathlib import Path

import subprocess
from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout

BASE_DIR = Path(__file__).parent
AUTH_FILE = BASE_DIR / "auth.json"
CRONOMETER_URL = "https://cronometer.com"


# ── Credentials ────────────────────────────────────────────────────────────────

def load_credentials() -> tuple[str, str]:
    with open(AUTH_FILE) as f:
        data = json.load(f)
    creds = data["http-basic"][CRONOMETER_URL]
    return creds["username"], creds["password"]


# ── Ingredient breakdown via Claude ────────────────────────────────────────────

PORTION_SCALE = {"small": 0.75, "normal": 1.0, "generous": 1.25, "large": 1.5}


def breakdown_ingredients(meal_description: str, portion: str = "normal") -> dict:
    """Ask Claude to break the meal into individual Cronometer-searchable components."""
    scale = PORTION_SCALE.get(portion, 1.0)
    portion_note = (
        f"Portion size: {portion} (scale all gram amounts by {scale:.2f} — "
        f"{'slightly less than' if scale < 1 else 'slightly more than' if scale > 1 else ''} a standard canteen serving)."
        if portion != "normal" else "Portion size: normal (standard canteen serving)."
    )
    prompt = (
        "You are a registered dietitian with expertise in German/European cafeteria food.\n"
        "Break this meal into individual components for Cronometer food diary tracking.\n"
        "Use English ingredient names that Cronometer's USDA/NCCDB food database would recognise — "
        "prefer generic names (e.g. 'hamburger bun white' over brand names).\n"
        f"{portion_note}\n\n"
        f"Meal: {meal_description}\n\n"
        "Reply with ONLY a valid JSON object, no markdown, no explanation:\n"
        '{"meal_name":"<short meal name>",'
        '"ingredients":['
        '{"search_name":"<English name for Cronometer search, 2-4 words, generic>",'
        '"amount_g":<number>,'
        '"calories":<integer>,'
        '"protein_g":<number>,'
        '"fat_g":<number>,'
        '"carbs_g":<number>,'
        '"fiber_g":<number>,'
        '"sugar_g":<number>,'
        '"sodium_mg":<number>}'
        "]}"
    )

    result = subprocess.run(["claude", "-p", prompt], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"claude CLI failed (exit {result.returncode}):\n"
            f"stderr: {result.stderr.strip() or '(empty)'}"
        )
    text = result.stdout.strip()
    if "```" in text:
        text = re.sub(r"```\w*\n?", "", text).strip()
    return json.loads(text)


# ── Display ────────────────────────────────────────────────────────────────────

def display_breakdown(data: dict) -> None:
    ings = data["ingredients"]
    total = sum(i["calories"] for i in ings)
    W = 72
    print(f"\n┌{'─'*W}┐")
    print(f"│  {data['meal_name'][:W-2]:<{W-2}}│")
    print(f"├{'─'*34}┬{'─'*7}┬{'─'*7}┬{'─'*7}┬{'─'*7}┬{'─'*6}┤")
    print(f"│  {'Ingredient':<32}│  kcal │  Prot │   Fat │  Carb │    g │")
    print(f"├{'─'*34}┼{'─'*7}┼{'─'*7}┼{'─'*7}┼{'─'*7}┼{'─'*6}┤")
    for ing in ings:
        n = ing["search_name"][:31]
        print(f"│  {n:<31} │{ing['calories']:>6} │{ing['protein_g']:>6.1f} │{ing['fat_g']:>6.1f} │{ing['carbs_g']:>6.1f} │{ing['amount_g']:>5} │")
    print(f"├{'─'*34}┼{'─'*7}┼{'─'*7}┼{'─'*7}┼{'─'*7}┼{'─'*6}┤")
    print(f"│  {'TOTAL':<31} │{total:>6} │{'':>6} │{'':>6} │{'':>6} │{'':>5} │")
    print(f"└{'─'*34}┴{'─'*7}┴{'─'*7}┴{'─'*7}┴{'─'*7}┴{'─'*6}┘")


# ── Cronometer browser automation ──────────────────────────────────────────────

SESSION_FILE = BASE_DIR / ".session.json"


async def cronometer_add(
    username: str,
    password: str,
    data: dict,
    log_date: str,
    visible: bool,
    debug: bool = False,
    meal_section: str = "Lunch",
) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=not visible,
            args=["--disable-dev-shm-usage"],
        )
        ctx_kwargs: dict = {"viewport": {"width": 1280, "height": 900}, "locale": "en-US"}
        if SESSION_FILE.exists():
            ctx_kwargs["storage_state"] = json.loads(SESSION_FILE.read_text())
            print("  Loaded saved session.", flush=True)
        ctx  = await browser.new_context(**ctx_kwargs)
        page = await ctx.new_page()


        async def shot(name: str) -> None:
            if not debug:
                return
            p = BASE_DIR / f"debug_{name}.png"
            await page.screenshot(path=str(p), full_page=True)
            print(f"  [screenshot] debug_{name}.png", flush=True)

        try:
            await _login(page, username, password, ctx)
            await shot("1_after_login")
            await _navigate_diary(page, log_date)
            await shot("2_diary")

            ingredients = data["ingredients"]
            for idx, ing in enumerate(ingredients):
                print(f"\n  [{idx+1}/{len(ingredients)}] {ing['search_name']} ({ing['amount_g']}g, {ing['calories']} kcal)", flush=True)
                await _add_one_ingredient(page, ing, shot, idx, meal_section=meal_section)

            await shot("final")
            print(f"\n✓  {len(ingredients)} ingredient(s) added to Cronometer diary!", flush=True)
            # Clean up any leftover debug files from previous runs
            for f in BASE_DIR.glob("debug_*.png"):
                f.unlink(missing_ok=True)
            for f in BASE_DIR.glob("debug_*.html"):
                f.unlink(missing_ok=True)

        except Exception as exc:
            # Always save an error screenshot regardless of debug flag
            p = BASE_DIR / "debug_error.png"
            await page.screenshot(path=str(p), full_page=True)
            print(f"\nError: {exc}", file=sys.stderr, flush=True)
            print(f"Screenshot saved → debug_error.png", file=sys.stderr, flush=True)
            raise
        finally:
            if visible:
                print("\nPress Enter to close the browser…")
                await asyncio.get_event_loop().run_in_executor(None, input)
            await browser.close()


async def _login(page: Page, username: str, password: str, ctx=None) -> None:
    print("Logging in…", flush=True)
    resp = await page.goto(f"{CRONOMETER_URL}/login/", wait_until="domcontentloaded", timeout=60_000)
    print(f"  HTTP {resp.status if resp else '?'}", flush=True)

    # Give the page up to 20s to show either the GWT app or the login form
    try:
        await page.wait_for_selector(
            'a.btn-sidebar:has-text("Diary"), input#username',
            state="visible", timeout=20_000
        )
    except PWTimeout:
        pass  # Neither appeared — fall through to login attempt anyway

    if await page.locator('a.btn-sidebar:has-text("Diary")').is_visible():
        print("  Session still valid — skipping login.", flush=True)
        return

    # If we landed on the Webflow marketing homepage (session cookies don't load the app
    # when navigating directly), go to the login page explicitly
    if "/login" not in page.url:
        await page.goto(f"{CRONOMETER_URL}/login/", wait_until="domcontentloaded", timeout=30_000)
        await page.locator("input#username").wait_for(state="visible", timeout=15_000)

    # Need to log in
    await page.locator("input#username").fill(username)
    await page.locator("input#password").fill(password)
    await page.locator("button#login-button").click()

    try:
        await page.wait_for_url(
            lambda url: "cronometer.com" in url and "/login" not in url,
            timeout=25_000,
        )
    except PWTimeout:
        err_loc = page.locator('p:has-text("Too Many"), div:has-text("Too Many"), [class*="error"]').first
        if await err_loc.count() > 0:
            try:
                msg = await err_loc.inner_text(timeout=2_000)
                raise RuntimeError(f"Cronometer login blocked: {msg.strip()!r}")
            except Exception:
                pass
        raise RuntimeError(
            f"Login timed out (still on {page.url}). "
            "Cronometer may have rate-limited this account — wait a few minutes and try again."
        )

    # Wait for GWT app to fully load, then save session
    await page.locator('a.btn-sidebar:has-text("Diary")').wait_for(state="visible", timeout=20_000)
    print(f"  Logged in → {page.url}", flush=True)
    if ctx:
        SESSION_FILE.write_text(json.dumps(await ctx.storage_state()))
        print("  Session saved.", flush=True)


async def _dismiss_popups(page: Page) -> None:
    """Remove the cookie consent overlay. Never touches GWT's own popup panels."""
    removed = await page.evaluate("""() => {
        const removed = [];
        // Click Accept to record consent server-side so it stops appearing
        const accept = Array.from(document.querySelectorAll('button'))
            .find(b => ['accept','accept all','i accept'].includes(b.innerText?.trim().toLowerCase()));
        if (accept) { accept.click(); removed.push('accepted:' + accept.innerText.trim()); }
        // Only remove the ncmp consent overlay — never GWT's own popup panels
        const ncmp = document.getElementById('ncmp__tool');
        if (ncmp) { ncmp.remove(); removed.push('removed:#ncmp__tool'); }
        return removed;
    }""")
    if removed:
        print(f"  Popups: {removed}", flush=True)
        await page.wait_for_timeout(400)


async def _navigate_diary(page: Page, log_date: str) -> None:
    print(f"Opening diary for {log_date}…", flush=True)
    diary_link = page.locator('a.btn-sidebar:has-text("Diary")').first
    await diary_link.wait_for(state="visible", timeout=15_000)
    await diary_link.click()
    await page.wait_for_selector('button.button-panel-btn', state="attached", timeout=15_000)
    await _dismiss_popups(page)
    await page.wait_for_timeout(500)

    # Navigate to the correct date if not today
    today = date.today().isoformat()
    if log_date != today:
        await _navigate_to_date(page, log_date)

    print(f"  Diary loaded. URL: {page.url}", flush=True)


async def _navigate_to_date(page: Page, target_date: str) -> None:
    """Click the prev/next arrows in the diary header to reach the target date."""
    from datetime import datetime, timedelta

    target = datetime.fromisoformat(target_date).date()
    today  = date.today()
    delta  = (target - today).days  # negative = past, positive = future

    if delta == 0:
        return

    arrow = "button.diary-nav-forward" if delta > 0 else "button.diary-nav-back"
    # Cronometer uses left/right chevron buttons next to the date header
    # Try a few selector patterns since GWT class names vary
    prev_sel = '[title="Previous Day"], .diary-nav-back, button:has-text("chevron_left")'
    next_sel = '[title="Next Day"], .diary-nav-forward, button:has-text("chevron_right")'
    sel = next_sel if delta > 0 else prev_sel
    clicks = abs(delta)

    print(f"  Navigating {clicks} day(s) {'forward' if delta > 0 else 'back'} to {target_date}…", flush=True)
    for _ in range(clicks):
        nav_btn = page.locator(sel).first
        try:
            await nav_btn.wait_for(state="visible", timeout=3_000)
            await nav_btn.click()
            await page.wait_for_timeout(800)
        except PWTimeout:
            # Try via JS as fallback
            clicked = await page.evaluate(f"""() => {{
                const btns = Array.from(document.querySelectorAll('button, [role="button"]'));
                const b = btns.find(b => b.offsetParent !== null && (
                    b.title?.includes('{"Next" if delta > 0 else "Previous"}') ||
                    b.innerText?.trim() === '{"chevron_right" if delta > 0 else "chevron_left"}'
                ));
                if (b) {{ b.click(); return true; }}
                return false;
            }}""")
            if not clicked:
                print(f"  Warning: could not navigate to {target_date}, logging to current date.", flush=True)
                return
            await page.wait_for_timeout(800)


async def _open_food_dialog(page: Page) -> None:
    """Open the 'Add Food to Diary' dialog."""
    await _dismiss_popups(page)
    # Click the FOOD button via JS — targets the first visible one regardless of which
    # panel it lives in (main bar vs hidden sidebar duplicate)
    await page.evaluate("""() => {
        const btn = Array.from(document.querySelectorAll('button.button-panel-btn'))
            .find(b => b.offsetParent !== null && b.innerText.trim() === 'FOOD');
        if (btn) btn.click();
    }""")
    await page.locator('input[placeholder="Search all foods & recipes..."]').first.wait_for(
        state="visible", timeout=8_000
    )
    await page.wait_for_timeout(500)


def _search_fallbacks(name: str) -> list[str]:
    """
    Return progressively simpler/alternative search terms to try before giving up.
    Asks Claude for DB-friendly alternatives so we prefer real DB entries over custom foods.
    """
    words = name.split()
    # Start with the original + simple word truncations
    candidates = [name]
    if len(words) >= 3:
        candidates.append(" ".join(words[:2]))
    if len(words) >= 2:
        candidates.append(words[0])

    # Ask Claude for alternative USDA/NCCDB-style names
    try:
        result = subprocess.run(
            ["claude", "-p",
             f"A user wants to find '{name}' in the Cronometer food database (USDA/NCCDB). "
             "Suggest 3 alternative generic English search terms that are likely to match "
             "real entries in the USDA food database. Order from most specific to most generic. "
             "Reply with ONLY a JSON array of strings, no explanation: "
             '[\"term1\", \"term2\", \"term3\"]'],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            text = result.stdout.strip()
            if "```" in text:
                text = re.sub(r"```\w*\n?", "", text).strip()
            alternatives = json.loads(text)
            if isinstance(alternatives, list):
                candidates.extend(alternatives)
    except Exception:
        pass  # Silently fall back to simple truncation if Claude call fails

    # Deduplicate while preserving order
    seen: set = set()
    return [c for c in candidates if c and not (c.lower() in seen or seen.add(c.lower()))]


async def _search_and_pick(page: Page, search_term: str) -> bool:
    """Search for search_term, return True if results appeared."""
    await page.evaluate(f"""() => {{
        const inputs = Array.from(document.querySelectorAll('input[placeholder="Search all foods & recipes..."]'))
            .filter(e => e.offsetParent !== null);
        if (inputs[0]) {{
            inputs[0].value = {json.dumps(search_term)};
            inputs[0].dispatchEvent(new Event('input', {{bubbles:true}}));
        }}
    }}""")
    await page.evaluate("() => document.querySelector('button.food-search-btn')?.click()")
    await page.wait_for_timeout(2_500)
    keyword = search_term.split()[0]
    return await page.locator(f'td:has-text("{keyword}")').count() > 0


async def _add_one_ingredient(page: Page, ing: dict, shot, idx: int, meal_section: str = "Lunch") -> None:
    """Add one ingredient: try progressively simpler DB searches, then custom food."""
    name     = ing["search_name"]
    target_g = float(ing["amount_g"])

    await _open_food_dialog(page)
    await shot(f"ing{idx}_dialog")

    # ── Try search terms from most specific to simplest ───────────────────
    found_term = None
    for term in _search_fallbacks(name):
        print(f"    Searching: {term!r}", flush=True)
        if await _search_and_pick(page, term):
            found_term = term
            break

    await shot(f"ing{idx}_results")

    if found_term:
        keyword = found_term.split()[0]
        print(f"    Found — clicking first match for {found_term!r}.", flush=True)

        # Click result row — overlays are removed so Playwright click works
        result_row = page.locator(f'td:has-text("{keyword}")').first
        await result_row.click()
        await page.wait_for_timeout(1_200)
        await _dismiss_popups(page)  # remove any popup that appeared after click
        await shot(f"ing{idx}_selected")

        # ── Diary Group → target meal section ──────────────────────────────
        group_btn = page.locator('button.dropdown-btn:has-text("Uncategorized")').first
        if await group_btn.count() > 0:
            await group_btn.click()
            await page.wait_for_timeout(300)
            await page.locator(f'.dropdown-item:text-is("{meal_section}")').first.click()
            await page.wait_for_timeout(300)

        # ── Serving quantity ────────────────────────────────────────────────
        serving_text = await page.evaluate("""() => {
            const btns = Array.from(document.querySelectorAll('button.dropdown-btn.dropdown-toggle'));
            const s = btns.find(b => /\\d+\\s*g/.test(b.innerText));
            return s ? s.innerText.trim() : null;
        }""")
        qty_val = "1"
        if serving_text:
            m = re.search(r"(\d+(?:\.\d+)?)\s*g\b", serving_text)
            if m:
                serving_g = float(m.group(1))
                qty_val = f"{target_g / serving_g:.2f}"
                print(f"    Serving: {serving_text!r} → qty={qty_val}", flush=True)
        # Set qty via JS (the input has no name/id, only its numeric value distinguishes it)
        await page.evaluate(f"""() => {{
            const inputs = Array.from(document.querySelectorAll('input.gwt-TextBox'))
                .filter(e => e.offsetParent !== null && /^[\\d.]+$/.test(e.value?.trim()));
            if (inputs.length) {{
                const inp = inputs[inputs.length - 1];
                inp.value = {json.dumps(qty_val)};
                inp.dispatchEvent(new Event('input', {{bubbles:true}}));
                inp.dispatchEvent(new Event('change', {{bubbles:true}}));
            }}
        }}""")
        await page.wait_for_timeout(300)

        # ── ADD TO DIARY ────────────────────────────────────────────────────
        add_btn = page.locator('button:has-text("ADD TO DIARY")').first
        await add_btn.wait_for(state="visible", timeout=6_000)
        await add_btn.click()

        # Confirm: dialog should close (ADD TO DIARY button disappears)
        try:
            await page.wait_for_function(
                """() => !document.querySelector('button[class*="btn-flat-jungle-green"]:not([style*="display: none"])')
                    ?.innerText?.includes('ADD TO DIARY')""",
                timeout=5_000,
            )
        except PWTimeout:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(500)

        print(f"    Added ({found_term!r}).", flush=True)

    else:
        # ── No DB result even with simpler terms → custom food page ──────────
        print(f"    Not in DB — creating custom food.", flush=True)
        await _create_custom_food(page, ing, shot, idx)


async def _create_custom_food(page: Page, ing: dict, shot, idx: int) -> None:
    """
    Navigate to /foods/custom (the full-page custom food creator),
    fill in nutrition facts, save, then navigate back to the diary.
    """
    # The "Custom Food" link in the no-results message goes to the custom food creator
    clicked = await page.evaluate("""() => {
        const link = Array.from(document.querySelectorAll('a'))
            .find(a => a.innerText?.trim().toLowerCase() === 'custom food');
        if (link) { link.click(); return true; }
        return false;
    }""")
    if not clicked:
        # Fall back: navigate directly
        await page.goto("https://cronometer.com/foods/custom", wait_until="domcontentloaded")

    await page.wait_for_timeout(3_000)
    await shot(f"ing{idx}_custom_form")

    # The custom food form has inputs next to label cells.
    # We use a robust JS fill that finds inputs by their adjacent label text.
    async def js_fill(label: str, value: str) -> None:
        await page.evaluate(f"""() => {{
            function setText(inp, val) {{
                const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value').set;
                nativeInputValueSetter.call(inp, val);
                inp.dispatchEvent(new Event('input', {{bubbles:true}}));
                inp.dispatchEvent(new Event('change', {{bubbles:true}}));
            }}
            // Find td/label/div containing exactly this label text
            const labelEl = Array.from(document.querySelectorAll('td, label, div, span'))
                .filter(e => e.offsetParent !== null)
                .find(e => e.childElementCount === 0 && e.innerText?.trim() === {json.dumps(label)});
            if (!labelEl) return;
            const inp = labelEl.nextElementSibling?.querySelector?.('input')
                     || labelEl.parentElement?.nextElementSibling?.querySelector('input')
                     || labelEl.closest('tr')?.querySelector('input');
            if (inp) setText(inp, {json.dumps(str(value))});
        }}""")

    await js_fill("Food Name",         ing["search_name"])
    await js_fill("Serving Size",      str(ing["amount_g"]))
    await js_fill("Calories",          str(ing["calories"]))
    await js_fill("Protein (g)",       f"{ing['protein_g']:.1f}")
    await js_fill("Fat (g)",           f"{ing['fat_g']:.1f}")
    await js_fill("Carbohydrates (g)", f"{ing['carbs_g']:.1f}")
    await js_fill("Fibre (g)",         f"{ing['fiber_g']:.1f}")
    await js_fill("Sugar (g)",         f"{ing['sugar_g']:.1f}")
    await js_fill("Sodium (mg)",       f"{ing['sodium_mg']:.0f}")
    await page.wait_for_timeout(500)
    await shot(f"ing{idx}_custom_filled")

    # Click "ADD TO DIARY" on the custom food page
    added = await page.evaluate("""() => {
        const btn = Array.from(document.querySelectorAll('button'))
            .find(b => b.innerText?.trim() === 'ADD TO DIARY');
        if (btn) { btn.click(); return true; }
        return false;
    }""")
    if not added:
        # Try "Save Food" first, then come back to diary
        await page.evaluate("""() => {
            const btn = Array.from(document.querySelectorAll('button'))
                .find(b => /save/i.test(b.innerText));
            if (btn) btn.click();
        }""")
    await page.wait_for_timeout(2_000)

    # Navigate back to diary if we left it
    if "#diary" not in page.url:
        await page.goto("https://cronometer.com/#diary", wait_until="domcontentloaded")
        await page.wait_for_selector('button.button-panel-btn', state="attached", timeout=15_000)
        await _dismiss_popups(page)
        await page.wait_for_timeout(500)
        print(f"    Custom food added, back on diary.", flush=True)
    else:
        print(f"    Custom food added.", flush=True)


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Add company lunch to Cronometer.")
    parser.add_argument("meal", nargs="?", help="Meal description")
    parser.add_argument("--date", default=date.today().isoformat(), metavar="YYYY-MM-DD")
    parser.add_argument("--visible", action="store_true", help="Show browser window")
    parser.add_argument("--estimate-only", action="store_true")
    parser.add_argument("--debug", action="store_true",
                        help="Save debug screenshots at each step")
    parser.add_argument("--meal",
                        default="Lunch",
                        choices=["Breakfast", "Lunch", "Dinner", "Snacks"],
                        help="Diary section to log into (default: Lunch)")
    parser.add_argument("--portion",
                        default="normal",
                        choices=list(PORTION_SCALE.keys()),
                        help="Portion size hint (default: normal)")
    parser.add_argument("--nutrition-from-stdin", action="store_true",
                        help="Read ingredient JSON from stdin (used by add_meal.sh)")
    args = parser.parse_args()

    # ── Docker mode: JSON piped in from add_meal.sh ───────────────────────────
    if args.nutrition_from_stdin:
        raw = sys.stdin.read().strip()
        if "```" in raw:
            raw = re.sub(r"```\w*\n?", "", raw).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"Invalid JSON from stdin: {exc}", file=sys.stderr)
            sys.exit(1)
        display_breakdown(data)
        username, password = load_credentials()
        print("Starting Playwright automation…", flush=True)
        try:
            asyncio.run(cronometer_add(
                username, password, data, args.date,
                visible=False, debug=args.debug, meal_section=args.meal,
            ))
        except Exception as exc:
            print(f"\nAutomation failed: {exc}", file=sys.stderr, flush=True)
            sys.exit(1)
        return

    # ── Local mode: run Claude + Playwright directly on host ──────────────────
    if args.meal:
        meal = args.meal.strip()
    else:
        print("Paste the meal description (Enter twice when done):\n")
        lines: list[str] = []
        try:
            while True:
                line = input()
                if not line and lines:
                    break
                lines.append(line)
        except EOFError:
            pass
        meal = "\n".join(lines).strip()

    if not meal:
        print("Error: no meal description provided.", file=sys.stderr)
        sys.exit(1)

    print(f"\nBreaking down: {meal[:80]}… (portion: {args.portion})")
    try:
        data = breakdown_ingredients(meal, portion=args.portion)
    except (json.JSONDecodeError, KeyError) as exc:
        print(f"Could not parse Claude's response: {exc}", file=sys.stderr)
        sys.exit(1)

    display_breakdown(data)

    if args.estimate_only:
        return

    try:
        ans = input(f"\nLog these {len(data['ingredients'])} ingredients to Cronometer for {args.date}? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        return
    if ans in ("n", "no"):
        print("Aborted.")
        return

    username, password = load_credentials()
    try:
        asyncio.run(cronometer_add(
        username, password, data, args.date,
        visible=args.visible, debug=args.debug, meal_section=args.meal,
    ))
    except Exception:
        sys.exit(1)


if __name__ == "__main__":
    main()
