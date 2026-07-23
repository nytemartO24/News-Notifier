#!/usr/bin/env python3
"""
Scrapes every Hasbro-branded "Beyblade X" listing from amazon.se search
results and merges newly-found ASINs into products.txt (used by
track_delivery_date_playwright.py), skipping anything already in
blacklist.txt. Notifies Discord about newly discovered products.

Setup:
    pip install playwright beautifulsoup4 requests
    python -m playwright install chromium

Usage:
    python scrape_hasbro_catalog.py

Env vars:
    DISCORD_WEBHOOK_URL   optional; posts a message listing new products found
    HEADLESS              "true" (default) or "false" — set false to watch
                           the browser locally while debugging
"""

import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

SCRIPT_DIR = Path(__file__).resolve().parent
PRODUCTS_FILE = SCRIPT_DIR / "products.txt"
BLACKLIST_FILE = SCRIPT_DIR / "blacklist.txt"
CATALOG_LOG_FILE = SCRIPT_DIR / "hasbro_catalog.txt"  # full scrape dump, for reference/debugging

BASE_URL = "https://www.amazon.se/s"
SEARCH_URL = f"{BASE_URL}?{urlencode({'k': 'beyblade x', 'rh': 'p_89:Hasbro'})}"
MAX_PAGES = 10  # safety cap so a pagination bug can't loop forever

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"


def notify(message: str) -> None:
    print(message, flush=True)
    if DISCORD_WEBHOOK_URL:
        import requests
        try:
            requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
        except requests.RequestException as e:
            print(f"  (failed to send Discord notification: {e})", file=sys.stderr)


def load_asin_set(path: Path) -> set:
    if not path.exists():
        return set()
    result = set()
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        asin = line.strip().split("#")[0].strip()  # ignore inline comments too
        if asin:
            result.add(asin)
    return result


def dismiss_continue_shopping_interstitial(page) -> bool:
    """Amazon's soft click-through wall ('Fortsätt handla' / 'Continue
    shopping'), not an image CAPTCHA — detect via its form action and click
    through. Returns True if found and handled."""
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


def safe_goto(page, url: str) -> None:
    """Navigate, tolerating the 'Download is starting' error some requests
    can trigger (seen on a fresh session's first request)."""
    print(f"Navigating to: {url}", flush=True)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        if "Download is starting" in str(e):
            print("  navigation triggered a download, continuing anyway.", flush=True)
            page.wait_for_timeout(1000)
        else:
            raise
    page.wait_for_timeout(2000)


def scrape_page(page) -> list[dict]:
    html = page.content()
    soup = BeautifulSoup(html, "html.parser")

    results = []
    seen_asins = set()
    for card in soup.select("div[data-asin]"):
        asin = card.get("data-asin", "").strip()
        if not asin or asin in seen_asins:
            continue

        title_el = card.select_one("h2 span") or card.select_one("h2 a")
        title = title_el.get_text(strip=True) if title_el else "(title not found)"

        seen_asins.add(asin)
        results.append({"asin": asin, "title": title})

    return results


def has_next_page(page) -> bool:
    return page.locator("a.s-pagination-next:not(.s-pagination-disabled)").count() > 0


def go_to_next_page(page) -> None:
    page.locator("a.s-pagination-next").first.click()
    page.wait_for_load_state("domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)


def scrape_catalog() -> list[dict]:
    all_results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-SE",
        )
        page = context.new_page()

        # Cold-start fix: the very first request in a fresh context can get
        # an odd response from Amazon. Warm up with the bare homepage first.
        safe_goto(page, "https://www.amazon.se/")
        for _ in range(2):
            if not dismiss_continue_shopping_interstitial(page):
                break

        safe_goto(page, SEARCH_URL)
        for _ in range(2):
            if not dismiss_continue_shopping_interstitial(page):
                break

        for page_num in range(1, MAX_PAGES + 1):
            print(f"Scraping page {page_num}...", flush=True)
            page_results = scrape_page(page)
            print(f"  found {len(page_results)} product(s) on this page", flush=True)
            all_results.extend(page_results)

            if not has_next_page(page):
                print("  no further pages.", flush=True)
                break

            go_to_next_page(page)
            for _ in range(2):
                if not dismiss_continue_shopping_interstitial(page):
                    break
        else:
            print(f"Hit MAX_PAGES={MAX_PAGES} safety cap — there may be more results.", flush=True)

        browser.close()

    deduped = []
    seen = set()
    for item in all_results:
        if item["asin"] not in seen:
            seen.add(item["asin"])
            deduped.append(item)
    return deduped


def main() -> None:
    scraped = scrape_catalog()
    print(f"\nTotal unique products scraped: {len(scraped)}", flush=True)

    # Full dump for reference/debugging — always overwritten, not used as
    # the source of truth (products.txt is).
    with open(CATALOG_LOG_FILE, "w", encoding="utf-8") as f:
        f.write(f"# Scraped {time.strftime('%Y-%m-%d %H:%M:%S')} — {len(scraped)} unique ASINs\n\n")
        for item in scraped:
            f.write(f"{item['asin']}  # {item['title'][:70]}\n")

    known_asins = load_asin_set(PRODUCTS_FILE)
    blacklisted_asins = load_asin_set(BLACKLIST_FILE)

    new_items = [
        item for item in scraped
        if item["asin"] not in known_asins and item["asin"] not in blacklisted_asins
    ]

    if not new_items:
        print("No new products found.", flush=True)
        return

    print(f"Found {len(new_items)} new product(s):", flush=True)
    with open(PRODUCTS_FILE, "a", encoding="utf-8") as f:
        for item in new_items:
            print(f"  {item['asin']} — {item['title'][:70]}", flush=True)
            f.write(f"{item['asin']}  # {item['title'][:70]}\n")

    lines = "\n".join(f"• {item['title'][:70]} ({item['asin']})" for item in new_items)
    notify(f"🆕 {len(new_items)} new Hasbro Beyblade X product(s) found:\n{lines} @Nytemart")


if __name__ == "__main__":
    main()

