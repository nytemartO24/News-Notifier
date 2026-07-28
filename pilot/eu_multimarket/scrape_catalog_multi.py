#!/usr/bin/env python3
"""
LOCAL PILOT — EU multi-marketplace Beyblade X catalog scraper.

Extends scripts/scrape_hasbro_catalog.py to loop over multiple Amazon EU
marketplaces (see marketplaces.py) instead of just amazon.se. Standalone
pilot: reads/writes only under pilot/eu_multimarket/state/<market>/ —
does NOT touch scripts/products.txt, blacklist.txt, or any production
state. Run manually (no cron) while validating locale handling.

Setup:
    pip install -r pilot/eu_multimarket/requirements.txt
    python -m playwright install chromium

Usage:
    python pilot/eu_multimarket/scrape_catalog_multi.py [market ...]
    # e.g. python pilot/eu_multimarket/scrape_catalog_multi.py de fr
    # defaults to all configured markets (se, de, fr) if none given

Env vars:
    DISCORD_WEBHOOK_URL / DISCORD_USER_ID   only used with --send-discord
    HEADLESS                                 "true" (default) or "false"
"""

import argparse
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
from logging_setup import setup_logger
from marketplaces import MARKETPLACES

PILOT_DIR = Path(__file__).parent
HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
DISCORD_USER_ID = os.environ.get("DISCORD_USER_ID", "")
MAX_PAGES = 10

logger = setup_logger("catalog_multi")


def notify(message: str, send_discord: bool) -> None:
    logger.info(message)
    if not send_discord:
        logger.info("[dry-run] would notify Discord (pass --send-discord to actually post)")
        return
    if not DISCORD_WEBHOOK_URL:
        logger.warning("DISCORD_WEBHOOK_URL not set, skipping Discord post")
        return
    import requests

    content = f"<@{DISCORD_USER_ID}> {message}" if DISCORD_USER_ID else message
    try:
        requests.post(
            DISCORD_WEBHOOK_URL,
            json={"content": content, "allowed_mentions": {"parse": ["users"]}},
            timeout=10,
        )
    except requests.RequestException as e:
        logger.warning(f"failed to send Discord notification: {e}")


def load_asin_set(path: Path) -> set:
    if not path.exists():
        return set()
    result = set()
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        asin = line.strip().split("#")[0].strip()
        if asin:
            result.add(asin)
    return result


def dismiss_continue_shopping_interstitial(page) -> bool:
    try:
        if page.locator("form[action='/errors_page/validateCaptcha']").count() == 0:
            return False
        button = page.locator("form[action='/errors_page/validateCaptcha'] button[type='submit']")
        if button.count() == 0:
            return False
        button.first.click()
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        page.wait_for_timeout(1500)
        return True
    except Exception:
        return False


def safe_goto(page, url: str) -> None:
    logger.info(f"Navigating to: {url}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        if "Download is starting" in str(e):
            logger.info("  navigation triggered a download, continuing anyway.")
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


def scrape_marketplace(market: str) -> list[dict]:
    config = MARKETPLACES[market]
    base_url = f"https://www.{config['domain']}/s"
    search_url = f"{base_url}?{urlencode({'k': 'beyblade x', 'rh': config['brand_filter']})}"
    all_results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS, channel="chromium")
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale=config["locale"],
        )
        page = context.new_page()

        safe_goto(page, f"https://www.{config['domain']}/")
        for _ in range(2):
            if not dismiss_continue_shopping_interstitial(page):
                break

        safe_goto(page, search_url)
        for _ in range(2):
            if not dismiss_continue_shopping_interstitial(page):
                break

        for page_num in range(1, MAX_PAGES + 1):
            logger.info(f"[{market}] Scraping page {page_num}...")
            page_results = scrape_page(page)
            logger.info(f"[{market}]   found {len(page_results)} product(s) on this page")
            all_results.extend(page_results)

            if not has_next_page(page):
                logger.info(f"[{market}]   no further pages.")
                break

            go_to_next_page(page)
            for _ in range(2):
                if not dismiss_continue_shopping_interstitial(page):
                    break
        else:
            logger.warning(f"[{market}] Hit MAX_PAGES={MAX_PAGES} safety cap — there may be more results.")

        browser.close()

    deduped = []
    seen = set()
    for item in all_results:
        if item["asin"] not in seen:
            seen.add(item["asin"])
            deduped.append(item)
    return deduped


def run(markets: list[str], send_discord: bool) -> None:
    for market in markets:
        if market not in MARKETPLACES:
            logger.error(f"Unknown market '{market}' — choices are {list(MARKETPLACES)}")
            continue

        market_dir = PILOT_DIR / "state" / market
        market_dir.mkdir(parents=True, exist_ok=True)
        products_file = market_dir / "products.txt"
        catalog_log_file = market_dir / "hasbro_catalog.txt"

        logger.info(f"=== [{market}] scraping {MARKETPLACES[market]['domain']} ===")
        scraped = scrape_marketplace(market)
        logger.info(f"[{market}] Total unique products scraped: {len(scraped)}")

        with open(catalog_log_file, "w", encoding="utf-8") as f:
            f.write(f"# Scraped {time.strftime('%Y-%m-%d %H:%M:%S')} — {len(scraped)} unique ASINs\n\n")
            for item in scraped:
                f.write(f"{item['asin']}  # {item['title'][:70]}\n")

        known_asins = load_asin_set(products_file)
        new_items = [item for item in scraped if item["asin"] not in known_asins]

        if not new_items:
            logger.info(f"[{market}] No new products found.")
            continue

        logger.info(f"[{market}] Found {len(new_items)} new product(s):")
        with open(products_file, "a", encoding="utf-8") as f:
            for item in new_items:
                logger.info(f"  {item['asin']} — {item['title'][:70]}")
                f.write(f"{item['asin']}  # {item['title'][:70]}\n")

        lines = "\n".join(f"• {item['title'][:70]} ({item['asin']})" for item in new_items)
        notify(
            f"🆕 [{market.upper()}] {len(new_items)} new Hasbro Beyblade X product(s) found:\n{lines}",
            send_discord,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("markets", nargs="*", default=list(MARKETPLACES), help="Market codes to scan (default: all configured)")
    parser.add_argument("--send-discord", action="store_true", help="Actually post to Discord instead of just logging what would be sent")
    args = parser.parse_args()

    logger.info(f"=== START catalog_multi markets={args.markets} ===")
    try:
        run(args.markets, args.send_discord)
        logger.info("=== END catalog_multi (exit 0) ===")
    except Exception:
        logger.exception("=== END catalog_multi (exit 1) ===")
        sys.exit(1)
