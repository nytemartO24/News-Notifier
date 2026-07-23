#!/usr/bin/env python3
"""
Amazon.se delivery-date tracker (Playwright edition, v2).

Reads ASINs from products.txt (one per line, '#' for comments/blank lines
ignored) and watches each product's estimated delivery date, alerting only
when the date itself changes.

Setup:
    pip install playwright beautifulsoup4
    python -m playwright install chromium

Usage:
    Put ASINs in products.txt next to this script, one per line, e.g.:
        B0G4NGG6L9
        B0GSZWMT3N

    python track_delivery_date_playwright.py
"""

import json
import os
import re
import time
import sys
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
PRODUCTS_FILE = SCRIPT_DIR / "products.txt"
BLACKLIST_FILE = SCRIPT_DIR / "blacklist.txt"
STATE_FILE = SCRIPT_DIR / "delivery_state.json"
ERROR_LOG_FILE = SCRIPT_DIR / "errors.log"

# Set via repo secret + workflow env in GitHub Actions; falls back to empty
# (console-only) for local runs unless you export it yourself.
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# In GitHub Actions, run a single check-and-exit cycle — the workflow's cron
# schedule provides the interval instead of an internal sleep loop. Locally
# (no GITHUB_ACTIONS env var), keep the original persistent loop behavior.
RUN_ONCE = os.environ.get("GITHUB_ACTIONS") == "true" or os.environ.get("RUN_ONCE") == "true"

CHECK_INTERVAL_SECONDS = 1800  # only used in persistent (non-RUN_ONCE) mode

CANDIDATE_SELECTORS = [
    "#mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE",
    "#deliveryBlockMessage",
    "#contextualIngressPtLabel_deliveryShortDeliveryDate",
    "#deliveryMessageMirId",
    "#mir-layout-DELIVERY_BLOCK",
]

MONTHS = (
    # English
    "january|february|march|april|may|june|july|august|september|october|november|december|"
    # Swedish
    "januari|februari|mars|april|maj|juni|juli|augusti|september|oktober|november|december"
)

# Captures ONLY the date chunk itself, e.g. "21 January, 2027" or "21 januari 2027",
# not the surrounding sentence — so unrelated wording changes won't false-positive.
DATE_PATTERN = re.compile(
    rf"\d{{1,2}}\s+(?:{MONTHS})\.?,?\s*(?:\d{{4}})?",
    re.IGNORECASE,
)

# Phrases indicating the listing has no delivery estimate yet (pre-order with
# no confirmed date, coming soon, unavailable, etc). These are expected
# states, not errors — we record them as such instead of crashing or
# treating them as a parse failure.
NO_DATE_SIGNALS = [
    "release date",
    "coming soon",
    "not yet available",
    "date has not been announced",
    "this item cannot be shipped",
]

# Distinct from NO_DATE_SIGNALS: these have a price and were previously
# available, they're just temporarily out of stock right now.
OUT_OF_STOCK_SIGNALS = [
    "temporarily out of stock",
    "tillfälligt slut i lager",
    "we are working hard to be back in stock",
]


MONTH_LOOKUP = {
    m: i + 1
    for i, m in enumerate(
        [
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        ]
    )
}
MONTH_LOOKUP.update(
    {
        m: i + 1
        for i, m in enumerate(
            [
                "januari", "februari", "mars", "april", "maj", "juni",
                "juli", "augusti", "september", "oktober", "november", "december",
            ]
        )
    }
)


def parse_date_for_sorting(date_str: str):
    """Best-effort parse of our extracted date string into a sortable date.
    Returns None for non-date states (NO DATE YET / UNKNOWN) so those can be
    pushed to the end of the sorted table."""
    import datetime

    m = re.search(r"(\d{1,2})\s+([a-zA-ZåäöÅÄÖ]+)\.?,?\s*(\d{4})?", date_str)
    if not m:
        return None
    day, month_name, year = m.groups()
    month = MONTH_LOOKUP.get(month_name.lower())
    if not month:
        return None
    today = datetime.date.today()
    year = int(year) if year else today.year
    try:
        parsed = datetime.date(year, month, int(day))
    except ValueError:
        return None
    # If no year was given and the date already passed this year, assume next year.
    if not m.group(3) and parsed < today:
        parsed = parsed.replace(year=year + 1)
    return parsed


def log_error(context: str, exc: Exception) -> None:
    import traceback

    with open(ERROR_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} | {context} ---\n")
        f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    print(f"[error] {context}: {exc}  (see {ERROR_LOG_FILE.name})", file=sys.stderr)


def load_blacklist() -> set:
    if not BLACKLIST_FILE.exists():
        BLACKLIST_FILE.write_text(
            "# One Amazon.se ASIN per line to exclude from scraping/alerts.\n"
            "# Use this for items you've already bought.\n"
            "# Example:\n"
            "# B0G4NGG6L9\n"
        )
        return set()

    blacklist = set()
    for line in BLACKLIST_FILE.read_text(encoding="utf-8-sig").splitlines():
        asin = line.strip().split("#")[0].strip()
        if not asin:
            continue
        blacklist.add(asin)
    return blacklist


def load_products() -> list[dict]:
    if not PRODUCTS_FILE.exists():
        PRODUCTS_FILE.write_text(
            "# One Amazon.se ASIN per line. Lines starting with # are ignored.\n"
            "# Example:\n"
            "# B0G4NGG6L9\n"
        )
        print(f"Created {PRODUCTS_FILE.name} — add ASINs and rerun.", flush=True)
        return []

    raw_lines = PRODUCTS_FILE.read_text(encoding="utf-8-sig").splitlines()
    blacklist = load_blacklist()

    products = []
    skipped = 0
    for line in raw_lines:
        asin = line.strip().split("#")[0].strip()
        if not asin:
            continue
        if asin in blacklist:
            skipped += 1
            continue
        products.append({"asin": asin, "url": f"https://www.amazon.se/dp/{asin}"})

    skip_note = f" ({skipped} blacklisted, skipped)" if skipped else ""
    print(f"Loaded {len(products)} product(s) from {PRODUCTS_FILE.name}.{skip_note}", flush=True)
    return products


def dismiss_continue_shopping_interstitial(page) -> bool:
    """Amazon sometimes shows a soft click-through wall ('Fortsätt handla' /
    'Continue shopping') for traffic it's suspicious of — not an image
    CAPTCHA, just a button. Detect it via its distinctive form action and
    click through. Returns True if the interstitial was found and handled."""
    try:
        if page.locator("form[action='/errors_page/validateCaptcha']").count() == 0:
            return False
        button = page.locator(
            "form[action='/errors_page/validateCaptcha'] button[type='submit']"
        )
        if button.count() == 0:
            return False
        button.first.click()
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        page.wait_for_timeout(1500)
        return True
    except Exception:
        return False


def fetch_delivery_date(page, url: str) -> str:
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)

    # Click through the soft interstitial if present, then re-check up to
    # twice more in case it chains (rare, but cheap to guard against).
    for _ in range(2):
        if not dismiss_continue_shopping_interstitial(page):
            break

    html = page.content()
    soup = BeautifulSoup(html, "html.parser")

    text_blob = ""
    for selector in CANDIDATE_SELECTORS:
        el = soup.select_one(selector)
        if el and el.get_text(strip=True):
            text_blob = el.get_text(" ", strip=True)
            break

    if not text_blob:
        text_blob = soup.get_text(" ", strip=True)

    match = DATE_PATTERN.search(text_blob)
    if match:
        return re.sub(r"\s+", " ", match.group()).strip().rstrip(",")

    full_text_lower = soup.get_text(" ", strip=True).lower()

    for signal in OUT_OF_STOCK_SIGNALS:
        if signal in full_text_lower:
            return "OUT OF STOCK (temporarily unavailable, no delivery date yet)"

    for signal in NO_DATE_SIGNALS:
        if signal in full_text_lower:
            return "NO DATE YET (listing has no confirmed delivery estimate)"

    # Genuinely unexpected page layout — worth a debug dump, but only keep one
    # per ASIN (overwritten each time) so these don't pile up silently.
    Path(SCRIPT_DIR / f"debug_{url.rsplit('/', 1)[-1]}.html").write_text(html, encoding="utf-8")
    return "UNKNOWN — no date found, saved debug HTML"


def get_product_title(page) -> str:
    title = page.title()
    # Amazon titles are usually "Product Name : Amazon.se: Category" — trim the tail.
    return title.split(" : Amazon")[0].strip() if title else "Unknown product"


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def notify(message: str) -> None:
    print(message)
    if DISCORD_WEBHOOK_URL:
        import requests
        try:
            requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
        except requests.RequestException as e:
            print(f"  (failed to send Discord notification: {e})", file=sys.stderr)


MAX_NAME_LENGTH = 40


def truncate_name(name: str) -> str:
    return name if len(name) <= MAX_NAME_LENGTH else name[: MAX_NAME_LENGTH - 1].rstrip() + "…"


def check_once(page, products: list[dict], state: dict) -> dict:
    for product in products:
        asin, url = product["asin"], product["url"]
        try:
            current_date = fetch_delivery_date(page, url)
            name = get_product_title(page)
        except Exception as e:
            log_error(f"product {asin} ({url})", e)
            continue

        previous_date = state.get(asin, {}).get("date")
        if previous_date is not None and current_date != previous_date:
            old_parsed = parse_date_for_sorting(previous_date)
            new_parsed = parse_date_for_sorting(current_date)
            # Alert only when a real date newly appeared (item went from
            # out-of-stock/no-date to having an estimate) or moved earlier.
            # A date slipping later, or disappearing again, is not alerted.
            became_earlier_or_new = new_parsed is not None and (
                old_parsed is None or new_parsed < old_parsed
            )
            if became_earlier_or_new:
                notify(
                    f"📦 Delivery date moved earlier for **{name}**:\n"
                    f"  was: {previous_date}\n"
                    f"  now: {current_date}\n"
                    f"  {url} @Nytemart"
                )

        state[asin] = {"name": name, "date": current_date}

    # Build sorted summary table: earliest known delivery date first,
    # unknown/no-date items pushed to the bottom.
    rows = []
    for asin, info in state.items():
        if not any(p["asin"] == asin for p in products):
            continue  # skip stale entries no longer in products.txt
        sort_key = parse_date_for_sorting(info["date"])
        rows.append((sort_key, truncate_name(info["name"]), info["date"]))

    rows.sort(key=lambda r: (r[0] is None, r[0]))

    name_width = max((len(name) for _, name, _ in rows), default=4)
    name_width = max(name_width, len("Product"))
    separator = "-" * (name_width + 3 + 40)

    print(f"\n{'='*len(separator)}")
    print(f"DELIVERY SUMMARY — {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(separator)
    print(f"{'Product':<{name_width}} | Delivery")
    print(separator)
    for _, name, date_str in rows:
        print(f"{name:<{name_width}} | {date_str}")
    print(f"{'='*len(separator)}\n")

    return state


def main() -> None:
    products = load_products()
    if not products:
        print("No products loaded — exiting without starting browser.", flush=True)
        return

    state = load_state()

    with sync_playwright() as p:
        browser = None
        context = None
        page = None

        def start_browser():
            nonlocal browser, context, page
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="en-SE",
            )
            page = context.new_page()

        start_browser()

        # Same cold-start fix discovered while building the catalog scraper:
        # the very first request in a fresh browser context can get an odd
        # response from Amazon. Warm up with the bare homepage first.
        try:
            page.goto("https://www.amazon.se/", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)
            dismiss_continue_shopping_interstitial(page)
        except Exception as e:
            log_error("homepage warm-up", e)

        try:
            if RUN_ONCE:
                products = load_products()
                state = check_once(page, products, state)
                save_state(state)
            else:
                while True:
                    try:
                        products = load_products()  # reload each cycle so you can add items live
                        state = check_once(page, products, state)
                        save_state(state)
                    except Exception as e:
                        # Catches anything that escaped per-product handling, e.g.
                        # the browser/page itself crashing mid-run. Log it, restart
                        # the browser, and keep going instead of dying silently.
                        log_error("main loop (restarting browser)", e)
                        try:
                            browser.close()
                        except Exception:
                            pass
                        start_browser()

                    time.sleep(CHECK_INTERVAL_SECONDS)
        finally:
            try:
                browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
