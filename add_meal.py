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
    """Load credentials from env vars (preferred) or auth.json fallback."""
    import os
    user = os.environ.get("CRONOMETER_USER")
    password = os.environ.get("CRONOMETER_PASSWORD")
    if user and password:
        return user, password
    # Fallback to auth.json for backwards compatibility
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
) -> tuple[dict, list[float]]:
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
        await _install_consent_bypass(ctx)
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
            kcal_list: list[float] = []
            for idx, ing in enumerate(ingredients):
                print(f"\n  [{idx+1}/{len(ingredients)}] {ing['search_name']} ({ing['amount_g']}g, {ing['calories']} kcal)", flush=True)
                kcal = await _add_one_ingredient(page, ing, shot, idx, meal_section=meal_section)
                kcal_list.append(kcal)

            await shot("final")
            if debug:
                await _dump_diary_debug(page, BASE_DIR)
            diary_kcals = await _read_section_kcals(page, meal_section, len(ingredients))
            _print_logged_summary(data, kcal_list, diary_kcals)
            print(f"\n✓  {len(ingredients)} ingredient(s) added to Cronometer diary!", flush=True)
            # Clean up debug screenshots from this run (only when not in debug mode)
            if not debug:
                for f in BASE_DIR.glob("debug_*.png"):
                    f.unlink(missing_ok=True)
                for f in BASE_DIR.glob("debug_*.html"):
                    f.unlink(missing_ok=True)

            # Use actual diary values for the adjustment flow when available
            return data, diary_kcals if diary_kcals else kcal_list

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


def _http_login(username: str, password: str) -> dict:
    """
    Authenticate with Cronometer via direct HTTP (no browser needed).
    Returns a dict of cookies to inject into the Playwright context.

    Flow (same as crono by milldr):
      1. GET  /login/         → extract anticsrf token + AWSALB cookies
      2. POST /login          → submit credentials → receive JSESSIONID + sesnonce
    """
    import urllib.request
    import urllib.parse
    import http.cookiejar
    import ssl

    print("Authenticating via HTTP…", flush=True)
    ctx = ssl.create_default_context()
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx),
        urllib.request.HTTPCookieProcessor(jar),
    )
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,*/*",
        "Accept-Encoding": "identity",
    }

    # Step 1: GET login page — collect cookies + anticsrf token
    req = urllib.request.Request(f"{CRONOMETER_URL}/login/", headers=headers)
    with opener.open(req, timeout=15) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    m = re.search(r'name="anticsrf"\s+value="([^"]+)"', html)
    if not m:
        raise RuntimeError(
            "Could not extract anticsrf token from login page. "
            "Cronometer may have changed their login form."
        )
    anticsrf = m.group(1)

    # Step 2: POST credentials
    payload = urllib.parse.urlencode({
        "username": username,
        "password": password,
        "anticsrf": anticsrf,
    }).encode("utf-8")
    post_headers = {
        **headers,
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": f"{CRONOMETER_URL}/login/",
    }
    req2 = urllib.request.Request(
        f"{CRONOMETER_URL}/login", data=payload, headers=post_headers
    )
    with opener.open(req2, timeout=15) as resp:
        final_url = resp.url

    # Verify we're authenticated
    cookie_names = {c.name for c in jar}
    if "sesnonce" not in cookie_names and "JSESSIONID" not in cookie_names:
        raise RuntimeError(
            "Login failed — no session cookie received. "
            "Check your credentials in auth.json or re-run ./setup.sh."
        )

    # Convert cookiejar → Playwright storage_state format
    cookies = []
    for c in jar:
        cookies.append({
            "name":     c.name,
            "value":    c.value,
            "domain":   c.domain.lstrip("."),
            "path":     c.path or "/",
            "httpOnly": bool(c.has_nonstandard_attr("HttpOnly")),
            "secure":   bool(c.secure),
            "sameSite": "Lax",
        })

    print(f"  Authenticated (cookies: {', '.join(c['name'] for c in cookies)})", flush=True)
    return {"cookies": cookies, "origins": []}


async def _login(page: Page, username: str, password: str, ctx=None) -> None:
    """Inject HTTP-acquired cookies into the browser, then verify the app loads."""

    # Attempt HTTP login first (fast, no rate-limit risk)
    try:
        storage_state = _http_login(username, password)
        if ctx:
            await ctx.add_cookies(storage_state["cookies"])
            # Persist for next run
            SESSION_FILE.write_text(json.dumps(storage_state))
    except Exception as exc:
        print(f"  HTTP login failed ({exc}), falling back to browser login…", flush=True)
        storage_state = None

    # Navigate to the app — with cookies injected it should load directly
    await page.goto(f"{CRONOMETER_URL}/login/", wait_until="domcontentloaded", timeout=30_000)

    # Wait for GWT app or login form.
    # On timeout we may have landed on the marketing page (stale session cookies
    # caused a redirect to https://cronometer.com/ which has neither the app sidebar
    # nor the login form).  Don't raise here — fall through to browser login instead.
    try:
        await page.wait_for_selector(
            'a.btn-sidebar:has-text("Diary"), input#username',
            state="visible", timeout=25_000,
        )
    except PWTimeout:
        pass  # handled below — browser login will recover

    if await page.locator('a.btn-sidebar:has-text("Diary")').is_visible():
        print("  App loaded.", flush=True)
        return

    # Cookies weren't enough (or timed out on marketing page) — browser login fallback.
    # Delete any stale session so it doesn't interfere on the next run.
    SESSION_FILE.unlink(missing_ok=True)
    print("  Cookies not accepted, logging in via browser…", flush=True)
    if "/login" not in page.url:
        await page.goto(f"{CRONOMETER_URL}/login/", wait_until="domcontentloaded", timeout=30_000)

    await page.locator("input#username").fill(username)
    await page.locator("input#password").fill(password)
    await page.locator("button#login-button").click()

    try:
        await page.wait_for_url(
            lambda url: "cronometer.com" in url and "/login" not in url,
            timeout=25_000,
        )
    except PWTimeout:
        err_loc = page.locator('p:has-text("Too Many"), div:has-text("Too Many")').first
        if await err_loc.count() > 0:
            msg = await err_loc.inner_text(timeout=2_000)
            raise RuntimeError(f"Cronometer login blocked: {msg.strip()!r}")
        raise RuntimeError(
            "Login timed out. Cronometer may be rate-limiting — wait a few minutes."
        )

    await page.locator('a.btn-sidebar:has-text("Diary")').wait_for(state="visible", timeout=20_000)
    print(f"  Logged in via browser.", flush=True)
    if ctx:
        SESSION_FILE.write_text(json.dumps(await ctx.storage_state()))
        print("  Session saved.", flush=True)


_CONSENT_BYPASS_SCRIPT = """
// Suppress all Cronometer consent / CMP overlays.
//
// Strategy:
//   1. Lock __tcfapi / __cmp with Object.defineProperty (non-writable,
//      non-configurable) so the CMP script cannot overwrite our stub.
//   2. MutationObserver removes CMP DOM nodes the instant they appear.
(function () {
    // ── 1. Lock the IAB TCF / CMP stubs ──────────────────────────────────────
    const _ok = { gdprApplies: false, tcString: '', eventStatus: 'tcloaded',
                  cmpStatus: 'loaded', purposeOneTreatment: false, publisherCC: 'DE' };
    function _stub(cmd, ver, cb) { if (typeof cb === 'function') cb(_ok, true); }

    for (const name of ['__tcfapi', '__cmp', '__uspapi']) {
        try {
            Object.defineProperty(window, name, {
                value: _stub,
                writable: false,
                configurable: false,
            });
        } catch (e) { /* already defined non-configurable */ }
    }

    // ── 2. DOM kill function ──────────────────────────────────────────────────
    const CMP_IDS      = ['ncmp__tool', 'qc-cmp2-container', 'qc-cmp2-ui',
                          'sp_message_container'];
    const CMP_PREFIXES = ['qc-cmp', 'cmp2', 'sp_message'];

    function _killBanner() {
        for (const id of CMP_IDS) {
            const el = document.getElementById(id);
            if (el) { el.remove(); return; }
        }
        for (const el of document.querySelectorAll('div,aside,section')) {
            if (el.closest('.gwt-PopupPanel')) continue;
            const cls = (el.className || '') + ' ' + (el.id || '');
            if (CMP_PREFIXES.some(p => cls.includes(p))) { el.remove(); return; }
        }
        // Last resort: any fixed/absolute overlay with very high z-index
        // that is NOT a GWT popup and NOT Cronometer's own context menu
        for (const el of document.querySelectorAll('body > div')) {
            if (el.closest('.gwt-PopupPanel') || el.id.startsWith('gwt')) continue;
            if (el.classList.contains('popup-menu')) continue;
            const s = getComputedStyle(el);
            if ((s.position === 'fixed' || s.position === 'absolute') &&
                parseInt(s.zIndex) >= 10000) {
                el.remove(); return;
            }
        }
    }

    _killBanner();
    new MutationObserver(_killBanner).observe(
        document.documentElement, { childList: true, subtree: true }
    );
})();
"""


async def _install_consent_bypass(ctx) -> None:
    """Suppress CMP/consent overlays for every page in this browser context.

    - Registers a JS init-script (runs before page scripts on every navigation).
    - Blocks requests to the Quantcast CMP CDN at the network level so the
      script never loads in the first place.
    """
    await ctx.add_init_script(_CONSENT_BYPASS_SCRIPT)
    # Block Quantcast CMP CDN — the modal can't appear if the script never loads
    await ctx.route(
        re.compile(r"https?://cmp\.quantcast\.com/"),
        lambda route, _req: route.abort(),
    )


async def _dismiss_popups(page: Page) -> None:
    """Fallback: forcibly remove any consent overlay still present in the DOM."""
    removed = await page.evaluate("""() => {
        const removed = [];
        const accept = Array.from(document.querySelectorAll('button'))
            .find(b => ['accept','accept all','i accept'].includes(b.innerText?.trim().toLowerCase()));
        if (accept) { accept.click(); removed.push('accepted:' + accept.innerText.trim()); }
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
    # Dismiss any consent overlay that reappeared after the dialog opened
    await _dismiss_popups(page)
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
    # Scope to the search dialog so diary entries don't produce false positives
    dialog = page.locator(
        '.pretty-dialog:has(input[placeholder="Search all foods & recipes..."])'
    ).first
    return await dialog.locator(f'td:has-text("{keyword}")').count() > 0


async def _add_one_ingredient(page: Page, ing: dict, shot, idx: int, meal_section: str = "Lunch") -> float:
    """Add one ingredient; returns kcal actually logged (DB-calibrated when available)."""
    name      = ing["search_name"]
    target_g  = float(ing["amount_g"])
    target_cal = float(ing["calories"])

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

        # Dismiss any consent overlay right before clicking the result row,
        # then give the DOM time to settle.
        await _dismiss_popups(page)
        await page.wait_for_timeout(600)

        # Use Playwright's CDP force-click (real mouse events via DevTools Protocol).
        # IMPORTANT: scope every selector to the search dialog so we never
        # accidentally click a diary entry with the same text.
        # The dialog is the .pretty-dialog that contains the search input.
        dialog = page.locator(
            '.pretty-dialog:has(input[placeholder="Search all foods & recipes..."])'
        ).first

        click_succeeded = False
        for sel in [
            f'td.no-left-padding:has-text("{keyword}")',
            f'td[align="left"]:has-text("{keyword}")',
            f'td:has-text("{keyword}")',
        ]:
            try:
                loc = dialog.locator(sel).first
                if await loc.count() == 0:
                    continue
                await loc.scroll_into_view_if_needed(timeout=2_000)
                await loc.click(force=True, timeout=5_000)
                click_succeeded = True
                break
            except PWTimeout:
                continue

        if not click_succeeded:
            print(f"    Warning: could not click result row for {found_term!r}.", flush=True)

        # Wait for the food detail panel to appear (Add to Diary button is the signal)
        try:
            await page.locator('button.btn-flat-jungle-green:has-text("Add to Diary")').first.wait_for(
                state="visible", timeout=6_000
            )
        except PWTimeout:
            await page.wait_for_timeout(1_800)
        await _dismiss_popups(page)  # remove any popup that appeared after click
        await shot(f"ing{idx}_selected")

        # ── Diary Group → target meal section ──────────────────────────────
        group_btn = page.locator('button.dropdown-btn:has-text("Uncategorized")').first
        try:
            await group_btn.wait_for(state="visible", timeout=5_000)
            await group_btn.scroll_into_view_if_needed()
            await group_btn.click(force=True)
            await page.wait_for_timeout(300)
            item = page.locator(f'.dropdown-item:text-is("{meal_section}")').first
            await item.click(force=True)
            await page.wait_for_timeout(300)
            print(f"    Diary group → {meal_section}", flush=True)
        except PWTimeout:
            print(f"    (Diary group dropdown not found, skipping)", flush=True)

        # ── Serving quantity ────────────────────────────────────────────────
        # Strategy:
        #  1. Open dropdown, prefer pure "g" unit.
        #  2. If "g" selected: calibrate by entering 100g, reading Cronometer's
        #     displayed kcal for 100g, then computing grams = target_cal*100/db_kcal.
        #     This corrects for caloric-density differences between AI and the DB.
        #  3. If only a named serving with gram weight exists: calculate qty ratio.
        #  4. Fallback: qty = 1.
        using_gram_unit = False
        qty_val = "1"
        kcal_logged = target_cal  # updated below if DB calibration succeeds

        # Open the serving dropdown and capture the current button label
        current_serving = await page.evaluate("""() => {
            const btns = Array.from(document.querySelectorAll('button.dropdown-btn.dropdown-toggle'))
                .filter(b => b.offsetParent !== null &&
                        !(b.getAttribute('aria-labelledby') || '').includes('diaryGroup'));
            if (btns[0]) { btns[0].click(); return btns[0].innerText.trim(); }
            return null;
        }""")
        await page.wait_for_timeout(600)

        if current_serving is not None:
            dropdown_items = await page.evaluate("""() => {
                return Array.from(document.querySelectorAll('.dropdown-item'))
                    .filter(e => e.offsetParent !== null)
                    .map(e => e.innerText.trim());
            }""")
            print(f"    Serving: {current_serving!r} | options: {dropdown_items[:6]}", flush=True)

            # Prefer exact "g" unit; otherwise first named option with a gram weight
            chosen_text = None
            chosen_g = None
            for opt in dropdown_items:
                if re.fullmatch(r'\s*g\s*', opt, re.IGNORECASE):
                    chosen_text, chosen_g = opt, 1.0
                    break
            if chosen_text is None:
                for opt in dropdown_items:
                    m = re.search(r"(\d+(?:\.\d+)?)\s*g\b", opt)
                    if m:
                        chosen_text, chosen_g = opt, float(m.group(1))
                        break

            if chosen_text and chosen_g:
                try:
                    item_loc = page.locator('.dropdown-item').filter(
                        has_text=re.compile(r'^\s*' + re.escape(chosen_text) + r'\s*$')
                    ).first
                    await item_loc.click(force=True, timeout=3_000)
                    await page.wait_for_timeout(800)
                    using_gram_unit = (chosen_g == 1.0)
                    qty_val = str(target_g) if using_gram_unit else f"{target_g / chosen_g:.2f}"
                    print(f"    Unit → {chosen_text!r}", flush=True)
                except PWTimeout:
                    await page.keyboard.press("Escape")
                    print(f"    Dropdown click failed", flush=True)
            else:
                # No gram option — select the first available unit (do NOT press Escape,
                # which would close the entire food dialog, not just the dropdown).
                # Then estimate qty from the serving label's weight (g or ml).
                if dropdown_items:
                    try:
                        first_item = page.locator('.dropdown-item').filter(
                            has_text=re.compile(r'^\s*' + re.escape(dropdown_items[0]) + r'\s*$')
                        ).first
                        await first_item.click(force=True, timeout=3_000)
                        await page.wait_for_timeout(500)
                        serving_label = dropdown_items[0]
                    except PWTimeout:
                        await page.keyboard.press("Escape")
                        await page.wait_for_timeout(300)
                        serving_label = current_serving
                else:
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(300)
                    serving_label = current_serving

                # Try to parse gram weight from label: prefer explicit 'g', then treat ml as g
                gm = re.search(r"(\d+(?:\.\d+)?)\s*g\b", serving_label)
                if gm:
                    qty_val = f"{target_g / float(gm.group(1)):.2f}"
                else:
                    ml = re.search(r"(\d+(?:\.\d+)?)\s*ml\b", serving_label, re.IGNORECASE)
                    if ml:
                        qty_val = f"{target_g / float(ml.group(1)):.2f}"
                print(f"    No gram unit (options: {dropdown_items[:4]}), qty={qty_val}", flush=True)
        else:
            print(f"    Serving dropdown not found", flush=True)

        # Find the qty input: last visible, interactive input that is not the food search box
        qty_input_idx = await page.evaluate("""() => {
            const all = Array.from(document.querySelectorAll('input'));
            const vis = all.filter(e => e.offsetParent !== null
                                    && !e.placeholder?.toLowerCase().includes('search')
                                    && e.getAttribute('aria-hidden') !== 'true');
            const inp = vis[vis.length - 1];
            return inp ? all.indexOf(inp) : -1;
        }""")

        if qty_input_idx >= 0:
            qty_loc = page.locator('input').nth(qty_input_idx)
            await qty_loc.scroll_into_view_if_needed(timeout=2_000)

            if using_gram_unit and target_cal > 0:
                # ── Calorie calibration ─────────────────────────────────────────
                # Enter 100g as a reference quantity, read what Cronometer shows for
                # 100g (avoids rounding-to-zero issues with 1g), then compute the
                # gram amount that matches the AI calorie estimate exactly.
                await qty_loc.click(click_count=3, force=True)
                await qty_loc.press_sequentially("100", delay=40)
                await qty_loc.press("Tab")
                await page.wait_for_timeout(700)

                db_kcal_100 = await page.evaluate("""() => {
                    // Scope ENTIRELY to the food search dialog so we never read
                    // the background diary's section totals (e.g. "1320 kcal").
                    const dialog = document.querySelector(
                        '.pretty-dialog:has(input[placeholder="Search all foods & recipes..."])'
                    );
                    if (!dialog) return null;

                    // Strategy 1: find an element whose ENTIRE visible text is
                    // "NNN kcal" — the food detail panel's standalone kcal display.
                    for (const el of dialog.querySelectorAll('div,span,td,label,p')) {
                        if (!el.offsetParent) continue;
                        const text = (el.innerText || '').trim();
                        const m = text.match(/^([0-9]{1,4})\\s*kcal$/i);
                        if (m) {
                            const n = parseInt(m[1]);
                            if (n > 0 && n < 5000) return n;
                        }
                    }

                    // Strategy 2: walk up from "Add to Diary" but stop at the
                    // dialog boundary so we can never escape into the diary.
                    const addBtn = Array.from(dialog.querySelectorAll('button'))
                        .find(b => b.offsetParent !== null
                                && /add to diary/i.test(b.innerText?.trim()));
                    let el = addBtn?.parentElement;
                    for (let i = 0; i < 6 && el && dialog.contains(el); i++) {
                        const t = el.innerText || '';
                        const m = t.match(/([0-9]{1,4})\\s*kcal/i)
                               || t.match(/kcal\\s*([0-9]{1,4})/i);
                        if (m) {
                            const n = parseInt(m[1]);
                            if (n > 0 && n < 5000) return n;
                        }
                        el = el.parentElement;
                    }
                    return null;
                }""")

                if db_kcal_100 and db_kcal_100 > 0:
                    final_g = round(target_cal * 100 / db_kcal_100, 1)
                    kcal_logged = round(db_kcal_100 * final_g / 100, 1)
                    print(f"    Cal: {db_kcal_100} kcal/100g → {final_g}g for {int(target_cal)} kcal", flush=True)
                    qty_val = str(final_g)
                else:
                    print(f"    Cal: density read failed, using AI estimate {target_g}g", flush=True)
                    qty_val = str(target_g)

            # Enter the final quantity
            await qty_loc.click(click_count=3, force=True)
            await qty_loc.press_sequentially(qty_val, delay=40)
            await qty_loc.press("Tab")
            await page.wait_for_timeout(600)
            print(f"    Set qty={qty_val}g", flush=True)
        else:
            print(f"    Warning: qty input not found", flush=True)

        # ── ADD TO DIARY ────────────────────────────────────────────────────
        # The button DOM text is "Add to Diary"; has-text is case-insensitive.
        add_btn = page.locator('button.btn-flat-jungle-green:has-text("Add to Diary")').first
        await add_btn.wait_for(state="visible", timeout=8_000)
        await add_btn.click(force=True)

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
        return kcal_logged

    else:
        # ── No DB result even with simpler terms → custom food page ──────────
        print(f"    Not in DB — creating custom food.", flush=True)
        await _create_custom_food(page, ing, shot, idx)
        return target_cal


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


async def _dump_diary_debug(page: Page, base_dir: Path) -> None:
    """Save diary HTML + screenshot for DOM debugging."""
    (base_dir / "debug_diary.html").write_text(await page.content(), encoding="utf-8")
    await page.screenshot(path=str(base_dir / "debug_diary.png"), full_page=True)
    print("  [debug] Saved debug_diary.html + debug_diary.png", flush=True)


async def _read_section_kcals(page: Page, section: str, expected: int) -> list[float] | None:
    """Read actual kcal values for each food row in a diary section.

    Retries up to 4× (with 800 ms waits) to handle Cronometer's async diary updates.
    Returns a list of floats in DOM order, or None if the count never reaches expected.
    """
    js = f"""() => {{
        const ALL_SECTIONS = ['Uncategorized','Breakfast','Lunch','Dinner','Snacks'];
        const target = {json.dumps(section)};
        let inSection = false;
        const kcals = [];

        for (const tr of document.querySelectorAll('tr')) {{
            if (!tr.offsetParent) continue;

            if (!tr.querySelector('.icon-food')) {{
                const txt = (tr.innerText || '').trim();
                for (const s of ALL_SECTIONS) {{
                    if (txt.startsWith(s)) {{ inSection = s === target; break; }}
                }}
                continue;
            }}
            if (!inSection) continue;

            // Column layout: name | qty | unit | kcal_value | "kcal" | protein | ...
            // kcal_value can be integer or decimal ("18.4", "519", "193.2").
            // The literal cell "kcal" immediately follows the kcal value cell.
            const cells = Array.from(tr.querySelectorAll('td'))
                .filter(e => e.offsetParent !== null)
                .map(e => (e.innerText || '').trim());

            let found = null;
            for (let i = 1; i < cells.length - 1; i++) {{
                if (/^kcal$/i.test(cells[i + 1])) {{
                    const n = parseFloat(cells[i]);
                    if (!isNaN(n) && n >= 0) {{ found = Math.round(n); break; }}
                }}
            }}
            if (found !== null) kcals.push(found);
        }}
        return kcals;
    }}"""

    for attempt in range(4):
        result = await page.evaluate(js)
        if result and len(result) >= expected:
            return [float(v) for v in result[-expected:]]
        if attempt < 3:
            await page.wait_for_timeout(800)

    count = len(result) if result else 0
    print(f"  (diary read: got {count}/{expected} rows — using AI estimates)", flush=True)
    return None


def _print_logged_summary(
    data: dict,
    ai_kcals: list[float],
    diary_kcals: list[float] | None = None,
) -> None:
    ings      = data["ingredients"]
    ai_total  = sum(i["calories"] for i in ings)
    has_diary = diary_kcals and len(diary_kcals) == len(ings)
    log_kcals = diary_kcals if has_diary else ai_kcals
    log_total = sum(log_kcals)
    source    = "Cronometer" if has_diary else "AI est"
    W = 56
    print(f"\n  {'─'*W}")
    print(f"  {'Ingredient':<30}  {'AI est':>7}  {source:>10}")
    print(f"  {'─'*W}")
    for ing, ai_k, log_k in zip(ings, ai_kcals, log_kcals):
        diff     = int(log_k) - int(ai_k)
        diff_str = f"({diff:+d})" if diff != 0 else ""
        print(f"  {ing['search_name'][:29]:<30}  {int(ai_k):>6} kcal  {int(log_k):>9} kcal  {diff_str}")
    print(f"  {'─'*W}")
    diff_total = int(log_total) - int(ai_total)
    diff_str   = f"({diff_total:+d})" if diff_total != 0 else ""
    print(f"  {'TOTAL':<30}  {int(ai_total):>6} kcal  {int(log_total):>9} kcal  {diff_str}")
    print(f"  {'─'*W}")


def suggest_adjustments(data: dict, kcal_list: list[float], target_kcal: float) -> dict:
    """Ask Claude to rescale ingredient amounts to hit target_kcal."""
    total_logged = sum(kcal_list)
    ing_lines = "\n".join(
        f"  {ing['search_name']}: {ing['amount_g']}g, logged≈{kcal:.0f} kcal"
        for ing, kcal in zip(data["ingredients"], kcal_list)
    )
    prompt = (
        "You are a registered dietitian. A user tracked a meal in Cronometer. "
        f"Cronometer recorded {total_logged:.0f} kcal total; the user's target is {target_kcal:.0f} kcal.\n\n"
        f"Current ingredients (gram amounts and kcal as recorded by Cronometer):\n{ing_lines}\n\n"
        "Adjust gram amounts to hit the target. Scale the highest-calorie items the most. "
        "Keep each ingredient ≥ 5g. Recalculate all macros proportionally.\n"
        "Reply with ONLY a valid JSON object in this exact format, no explanation:\n"
        '{"meal_name":"<name>","ingredients":[{"search_name":"...","amount_g":<n>,"calories":<n>,'
        '"protein_g":<n>,"fat_g":<n>,"carbs_g":<n>,"fiber_g":<n>,"sugar_g":<n>,"sodium_mg":<n>}]}'
    )
    result = subprocess.run(["claude", "-p", prompt], capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"Claude CLI failed: {result.stderr.strip()}")
    text = result.stdout.strip()
    if "```" in text:
        text = re.sub(r"```\w*\n?", "", text).strip()
    return json.loads(text)


def _run_adjustment_flow(
    data: dict,
    kcal_list: list[float],
    username: str,
    password: str,
    log_date: str,
    section: str,
    visible: bool,
    debug: bool,
) -> None:
    total_logged = int(sum(kcal_list))
    try:
        ans = input(f"\nAdjust? Enter target kcal or press Enter to skip [{total_logged} kcal logged]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if not ans:
        return
    try:
        target_kcal = float(ans)
    except ValueError:
        print("Invalid target — skipping adjustment.", file=sys.stderr)
        return

    print(f"\nAsking Claude for adjustments ({total_logged} → {int(target_kcal)} kcal)…", flush=True)
    try:
        adjusted = suggest_adjustments(data, kcal_list, target_kcal)
    except Exception as exc:
        print(f"Could not get suggestions: {exc}", file=sys.stderr)
        return

    display_breakdown(adjusted)

    try:
        confirm = input(
            f"\nApply these adjustments? This will clear {section!r} and re-add. [Y/n]: "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        return
    if confirm in ("n", "no"):
        print("Adjustment cancelled.")
        return

    print(f"\nClearing {section!r}…", flush=True)
    try:
        removed = asyncio.run(cronometer_clear_section(
            username, password, section=section, log_date=log_date, debug=debug,
        ))
        print(f"  Removed {removed} entry/entries.", flush=True)
    except Exception as exc:
        print(f"Clear failed: {exc}", file=sys.stderr)
        return

    print("Re-adding adjusted ingredients…", flush=True)
    try:
        asyncio.run(cronometer_add(
            username, password, adjusted, log_date,
            visible=visible, debug=debug, meal_section=section,
        ))
    except Exception as exc:
        print(f"Re-add failed: {exc}", file=sys.stderr)
        sys.exit(1)


async def cronometer_clear_section(
    username: str,
    password: str,
    section: str,
    log_date: str,
    debug: bool = False,
) -> int:
    """Remove all food entries from a diary section. Returns number of entries removed."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage"],
        )
        ctx_kwargs: dict = {"viewport": {"width": 1280, "height": 900}, "locale": "en-US"}
        if SESSION_FILE.exists():
            ctx_kwargs["storage_state"] = json.loads(SESSION_FILE.read_text())
            print("  Loaded saved session.", flush=True)
        ctx = await browser.new_context(**ctx_kwargs)
        await _install_consent_bypass(ctx)
        page = await ctx.new_page()

        async def shot(name: str) -> None:
            p = BASE_DIR / f"debug_{name}.png"
            await page.screenshot(path=str(p), full_page=True)
            print(f"  Saved {p.name}", flush=True)

        try:
            await _login(page, username, password, ctx)
            await _navigate_diary(page, log_date)
            await _dismiss_popups(page)
            await shot("clear")

            total_removed = 0

            for _pass in range(30):  # safety cap
                # Return the actual DOM element so we can scroll it into view and
                # get fresh viewport coordinates — items below the fold have
                # getBoundingClientRect().y > viewport height, and mouse.click at
                # off-screen coordinates silently does nothing in Playwright.
                handle = await page.evaluate_handle(f"""() => {{
                    const ALL_SECTIONS = ['Uncategorized','Breakfast','Lunch','Dinner','Snacks'];
                    const target = {json.dumps(section)};
                    let inSection = false;

                    for (const tr of document.querySelectorAll('tr')) {{
                        if (!tr.offsetParent) continue;

                        if (!tr.querySelector('.icon-food')) {{
                            const txt = (tr.innerText || '').trim();
                            for (const s of ALL_SECTIONS) {{
                                if (txt.startsWith(s)) {{ inSection = s === target; break; }}
                            }}
                            continue;
                        }}

                        if (!inSection) continue;

                        // Prefer the food-name cell; fall back to full row
                        return tr.querySelector('td.no-left-padding')
                            || tr.querySelector('td[align="left"]')
                            || tr;
                    }}
                    return null;
                }}""")

                # evaluate_handle returns a JSHandle wrapping null when nothing found
                is_null = await handle.evaluate("el => el === null")
                if is_null:
                    break

                label = await handle.evaluate(
                    "el => (el.innerText || '').trim().split('\\n')[0]"
                )
                print(f"  Right-clicking: {label!r}", flush=True)

                # Scroll the element into the centre of the viewport, then get
                # fresh coordinates — must happen AFTER scrollIntoView settles.
                await handle.evaluate("el => el.scrollIntoView({block:'center', inline:'nearest'})")
                await page.wait_for_timeout(300)

                bbox = await handle.bounding_box()
                if not bbox:
                    print("  Could not get bounding box — skipping.", flush=True)
                    break

                cx = bbox["x"] + bbox["width"]  / 2
                cy = bbox["y"] + bbox["height"] / 2
                print(f"  Coordinates after scroll: ({cx:.0f}, {cy:.0f})", flush=True)

                await page.mouse.click(cx, cy, button="right")
                await page.wait_for_timeout(600)

                # Screenshot BEFORE any cleanup so we see exactly what appeared
                await shot(f"clear_ctx_raw_{_pass}")

                # Log every visible body-level popup to identify what's in the DOM
                popups = await page.evaluate("""() =>
                    Array.from(document.querySelectorAll('body > div, body > aside'))
                        .filter(e => e.offsetParent !== null)
                        .map(e => ({
                            id:    e.id || '(no id)',
                            cls:   (e.className || '').substring(0, 60),
                            z:     getComputedStyle(e).zIndex,
                            pos:   getComputedStyle(e).position,
                            text:  (e.innerText || '').trim().substring(0, 80),
                        }))
                """)
                print(f"  Body-level popups: {popups}", flush=True)

                # Nuke any CMP overlay — but explicitly spare Cronometer's popup-menu
                await page.evaluate("""() => {
                    for (const id of ['ncmp__tool','qc-cmp2-container','qc-cmp2-ui',
                                      'sp_message_container']) {
                        const el = document.getElementById(id);
                        if (el) { el.remove(); return; }
                    }
                    for (const el of document.querySelectorAll('body > div')) {
                        if (el.closest('.gwt-PopupPanel') || (el.id||'').startsWith('gwt')) continue;
                        if (el.classList.contains('popup-menu')) continue;
                        const s = getComputedStyle(el);
                        if ((s.position==='fixed'||s.position==='absolute') &&
                            parseInt(s.zIndex) >= 10000) { el.remove(); return; }
                    }
                }""")
                await page.wait_for_timeout(200)

                await shot(f"clear_ctx_{_pass}")

                # Click "Delete Selected Items" in the GWT context menu
                try:
                    delete_item = page.get_by_text("Delete Selected Items").first
                    await delete_item.wait_for(state="visible", timeout=4_000)
                    await delete_item.click(force=True)
                except PWTimeout:
                    print(
                        "  'Delete Selected Items' not found — context menu did not open.",
                        flush=True,
                    )
                    await page.keyboard.press("Escape")
                    break

                # Cronometer shows a "Delete Items? YES / NO" confirmation dialog
                try:
                    yes_btn = page.get_by_role("button", name="YES").first
                    await yes_btn.wait_for(state="visible", timeout=3_000)
                    await yes_btn.click(force=True)
                    print(f"  Confirmed deletion.", flush=True)
                except PWTimeout:
                    # No confirmation dialog — deletion may have gone through directly
                    pass

                await page.wait_for_timeout(1_000)
                await shot(f"clear_after_{_pass}")
                total_removed += 1
                print(f"  Deleted.", flush=True)

            return total_removed

        except Exception as exc:
            p = BASE_DIR / "debug_clear_error.png"
            await page.screenshot(path=str(p), full_page=True)
            print(f"\nError: {exc}", file=sys.stderr, flush=True)
            print("Screenshot saved → debug_clear_error.png", file=sys.stderr, flush=True)
            raise
        finally:
            await browser.close()


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Add company lunch to Cronometer.")
    parser.add_argument("meal", nargs="?", help="Meal description")
    parser.add_argument("--date", default=date.today().isoformat(), metavar="YYYY-MM-DD")
    parser.add_argument("--visible", action="store_true", help="Show browser window")
    parser.add_argument("--estimate-only", action="store_true")
    parser.add_argument("--debug", action="store_true",
                        help="Save debug screenshots at each step")
    parser.add_argument("--section",
                        default="Lunch",
                        choices=["Breakfast", "Lunch", "Dinner", "Snacks"],
                        help="Diary section to log into (default: Lunch)")
    parser.add_argument("--clear-section",
                        metavar="SECTION",
                        help="Delete all entries from this diary section and exit")
    parser.add_argument("--portion",
                        default="normal",
                        choices=list(PORTION_SCALE.keys()),
                        help="Portion size hint (default: normal)")
    parser.add_argument("--nutrition-from-stdin", action="store_true",
                        help="Read ingredient JSON from stdin (used by add_meal.sh)")
    parser.add_argument("--nutrition-from-file", metavar="PATH",
                        help="Read ingredient JSON from file (used by add_meal.sh for interactive mode)")
    args = parser.parse_args()

    # ── Clear-section mode ────────────────────────────────────────────────────
    if args.clear_section:
        username, password = load_credentials()
        try:
            removed = asyncio.run(cronometer_clear_section(
                username, password,
                section=args.clear_section,
                log_date=args.date,
                debug=args.debug,
            ))
        except Exception as exc:
            print(f"\nClear failed: {exc}", file=sys.stderr, flush=True)
            sys.exit(1)
        print(f"\n✓  Removed {removed} entry/entries from {args.clear_section} on {args.date}.", flush=True)
        return

    # ── Docker mode: JSON from file (used by add_meal.sh) ────────────────────
    if args.nutrition_from_file:
        try:
            raw = Path(args.nutrition_from_file).read_text().strip()
        except OSError as exc:
            print(f"Cannot read {args.nutrition_from_file}: {exc}", file=sys.stderr)
            sys.exit(1)
        if "```" in raw:
            raw = re.sub(r"```\w*\n?", "", raw).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"Invalid JSON: {exc}", file=sys.stderr)
            sys.exit(1)
        display_breakdown(data)
        username, password = load_credentials()
        print("Starting Playwright automation…", flush=True)
        try:
            data_out, kcal_list = asyncio.run(cronometer_add(
                username, password, data, args.date,
                visible=False, debug=args.debug, meal_section=args.section,
            ))
        except Exception as exc:
            print(f"\nAutomation failed: {exc}", file=sys.stderr, flush=True)
            sys.exit(1)
        # Write kcal results to mounted volume so add_meal.sh can run the
        # adjustment flow on the host (where claude CLI is available).
        kcal_output = {
            "meal_name": data_out.get("meal_name", ""),
            "ingredients": [
                {**ing, "cronometer_kcal": int(k)}
                for ing, k in zip(data_out["ingredients"], kcal_list)
            ],
            "total_cronometer": int(sum(kcal_list)),
        }
        (BASE_DIR / ".last_logged_kcals.json").write_text(json.dumps(kcal_output))
        return

    # ── Docker mode: JSON piped in from add_meal.sh (non-interactive) ─────────
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
            data_out, kcal_list = asyncio.run(cronometer_add(
                username, password, data, args.date,
                visible=False, debug=args.debug, meal_section=args.section,
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
        data_out, kcal_list = asyncio.run(cronometer_add(
            username, password, data, args.date,
            visible=args.visible, debug=args.debug, meal_section=args.section,
        ))
    except Exception:
        sys.exit(1)
    _run_adjustment_flow(
        data_out, kcal_list, username, password,
        args.date, args.section, args.visible, args.debug,
    )


if __name__ == "__main__":
    main()
