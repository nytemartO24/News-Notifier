#!/usr/bin/env python3
"""
LOCAL PILOT — EU multi-marketplace Beyblade X catalog scraper.

Extends scripts/scrape_hasbro_catalog.py to loop over multiple Amazon EU
marketplaces (see marketplaces.py) instead of just amazon.se, using a
Hasbro-brand, newest-first search:

    https://www.<domain>/s?k=beyblade+x&rh=p_123%3A219753&s=date-desc-rank&dc&language=en

("p_123:219753" is Amazon's brand-catalog id for Hasbro — a numeric id
shared across the EU marketplaces' unified catalog, unlike the
name-based p_89 filter this pilot used before. "language=en" asks for
English page text even on non-English domains.)

Blacklist model (deliberately inverted from a normal allowlist): the
blacklist is a SINGLE FILE SHARED ACROSS ALL MARKETS
(state/blacklist.txt), since ASINs are the same product across Amazon's
EU marketplaces — a product blacklisted after being seen on .se is
recognized as already-known on .de/.fr too, not re-flagged per market.

Each market still gets its own one-time FULL scan across every page the
first time it's run (tracked per-market via a
state/<market>/.baseline_done marker, independent of whether the shared
blacklist already exists from another market's run) — this matters
because a market can carry ASINs no other market's catalog has, which a
shared-but-already-seeded blacklist would otherwise cause to be missed.
Whether an ASIN needs adding to the blacklist during a baseline scan is
decided against EVERY line ever recorded for it (commented or not), not
just currently-active ones — otherwise a later market's baseline scan
would see a deliberately-un-blacklisted (commented-out) ASIN as "not yet
blacklisted" and silently add a duplicate active line for it, undoing
the un-blacklisting.

That baseline scan seeds BOTH the shared blacklist AND that market's own
products.txt with everything found, silently (no notifications) —
mirroring production, where blacklist.txt started as a literal copy of
products.txt. This matters: it's what makes "un-blacklist an item to
resume tracking it" actually work. Since track_delivery_multi.py excludes
any blacklisted ASIN from delivery tracking even when it's listed in
products.txt (see that file), commenting an item's whole line out of
state/blacklist.txt (prefix with '#') puts it back into the trackable
set — silently, no notification, since it was already known — because it
never left products.txt in the first place.

Every later run for an already-baselined market only needs page 1, since
results are sorted newest-first: anything there that's already in either
the shared blacklist or that market's products.txt is old news. Anything
in neither is a genuinely new arrival (never seen in any baseline or
prior run) — that gets logged/notified once and appended to products.txt,
deliberately NOT blacklisted, so it's immediately trackable and won't be
re-notified as "new" again (products.txt membership alone prevents that).

Standalone pilot: reads/writes only under
pilot/eu_multimarket/state/ — does NOT touch
scripts/products.txt, blacklist.txt, or any production state.

Setup:
    pip install -r pilot/eu_multimarket/requirements.txt
    python -m playwright install chromium

Usage:
    python pilot/eu_multimarket/scrape_catalog_multi.py [market ...]
    # e.g. python pilot/eu_multimarket/scrape_catalog_multi.py de fr
    # defaults to all configured markets (see marketplaces.py) if none given

Env vars:
    DISCORD_WEBHOOK_URL / DISCORD_USER_ID   only used with --send-discord
    HEADLESS                                 "true" (default) or "false"
"""

import argparse
import os
import re
import sys
import time
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
from logging_setup import setup_logger
from marketplaces import MARKETPLACES

PILOT_DIR = Path(__file__).parent
STATE_DIR = PILOT_DIR / "state"
BLACKLIST_FILE = STATE_DIR / "blacklist.txt"  # shared across all markets — see module docstring
HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
DISCORD_USER_ID = os.environ.get("DISCORD_USER_ID", "")
MAX_PAGES = 10  # safety cap for the initial full-catalog scan only

# Hasbro's brand-catalog id — believed constant across the EU unified
# catalog, but not independently confirmed on every marketplace here.
SEARCH_URL_TEMPLATE = (
    "https://www.{domain}/s?k=beyblade+x&rh=p_123%3A219753&s=date-desc-rank&dc&language=en"
)

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


ASIN_PATTERN = re.compile(r"^[A-Z0-9]{10}$")


def load_asin_set(path: Path) -> set:
    # A line whose whole content is commented out (starts with '#') has
    # nothing before the first '#', so it contributes no ASIN here — that's
    # exactly how you "un-blacklist" an item: prefix its entire line with
    # '#' and it drops out of this set.
    if not path.exists():
        return set()
    result = set()
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        asin = line.strip().split("#")[0].strip()
        if asin:
            result.add(asin)
    return result


def load_recorded_asin_set(path: Path) -> set:
    # Every ASIN that has EVER had a line written for it, whether it's
    # currently active or deliberately commented out. Used specifically
    # to decide whether to append a new blacklist line during a baseline
    # scan — checking only load_asin_set() (active-only) would treat an
    # ASIN someone deliberately un-blacklisted as "not yet blacklisted"
    # and silently re-add a duplicate active line for it the next time
    # any market's baseline scan finds that same (shared-catalog) ASIN,
    # undoing the un-blacklisting.
    if not path.exists():
        return set()
    result = set()
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            stripped = stripped[1:].strip()
        asin = stripped.split("#")[0].strip()
        if asin and ASIN_PATTERN.match(asin):
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


def scrape_marketplace(market: str, full_scan: bool) -> list[dict]:
    config = MARKETPLACES[market]
    search_url = SEARCH_URL_TEMPLATE.format(domain=config["domain"])
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

        max_pages = MAX_PAGES if full_scan else 1
        hit_cap = full_scan  # only meaningful (and only warned about) for a full scan
        for page_num in range(1, max_pages + 1):
            logger.info(f"[{market}] Scraping page {page_num}...")
            page_results = scrape_page(page)
            logger.info(f"[{market}]   found {len(page_results)} product(s) on this page")
            all_results.extend(page_results)

            if not full_scan:
                logger.info(f"[{market}]   page-1-only mode (newest-first sort) — done.")
                hit_cap = False
                break

            if not has_next_page(page):
                logger.info(f"[{market}]   no further pages.")
                hit_cap = False
                break

            go_to_next_page(page)
            for _ in range(2):
                if not dismiss_continue_shopping_interstitial(page):
                    break

        if hit_cap:
            logger.warning(f"[{market}] Hit MAX_PAGES={MAX_PAGES} safety cap — there may be more results.")

        browser.close()

    deduped = []
    seen = set()
    for item in all_results:
        if item["asin"] not in seen:
            seen.add(item["asin"])
            deduped.append(item)
    return deduped


def seed_blacklist_header_if_new() -> None:
    if BLACKLIST_FILE.exists():
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    BLACKLIST_FILE.write_text(
        "# Shared across ALL markets — an ASIN here is ignored on every\n"
        "# marketplace, not just the one it was first seen on. To be alerted\n"
        "# about a specific item again, comment out its WHOLE line (prefix with\n"
        "# '#') — it'll then be treated as not-blacklisted on the next run,\n"
        "# flagged once, and automatically re-added here so it doesn't repeat.\n\n"
    )


def run(markets: list[str], send_discord: bool) -> None:
    seed_blacklist_header_if_new()

    for market in markets:
        if market not in MARKETPLACES:
            logger.error(f"Unknown market '{market}' — choices are {list(MARKETPLACES)}")
            continue

        market_dir = STATE_DIR / market
        market_dir.mkdir(parents=True, exist_ok=True)
        products_file = market_dir / "products.txt"
        catalog_log_file = market_dir / "hasbro_catalog.txt"
        baseline_marker = market_dir / ".baseline_done"

        # Per-market, independent of whether the shared blacklist already
        # exists from another market's run — see module docstring.
        full_scan = not baseline_marker.exists()
        if full_scan:
            logger.info(f"[{market}] No baseline yet for this market — doing a full initial scan.")

        logger.info(f"=== [{market}] scraping {MARKETPLACES[market]['domain']} ===")
        scraped = scrape_marketplace(market, full_scan=full_scan)
        logger.info(f"[{market}] Total unique products scraped this run: {len(scraped)}")

        with open(catalog_log_file, "w", encoding="utf-8") as f:
            f.write(f"# Scraped {time.strftime('%Y-%m-%d %H:%M:%S')} — {len(scraped)} unique ASINs\n\n")
            for item in scraped:
                f.write(f"{item['asin']}  # {item['title'][:70]}\n")

        blacklisted_asins = load_asin_set(BLACKLIST_FILE)

        if full_scan:
            # Seed BOTH files with everything found — mirrors production,
            # where blacklist.txt started as a literal copy of the full
            # products.txt. products.txt makes every item trackable;
            # blacklist.txt excludes it by default. Commenting an item's
            # line out of blacklist.txt then does exactly what it's meant
            # to: it's still in products.txt, so it resumes being
            # delivery-tracked, without needing a separate re-seed step.
            #
            # Checked against EVERY recorded ASIN (commented or not), not
            # just the currently-active blacklist — otherwise an ASIN
            # someone deliberately un-blacklisted looks "not yet
            # blacklisted" to a later market's baseline scan (since ASINs
            # are shared across the EU catalog) and gets a duplicate
            # active line silently re-added, undoing the un-blacklisting.
            recorded_asins = load_recorded_asin_set(BLACKLIST_FILE)
            new_to_blacklist = [item for item in scraped if item["asin"] not in recorded_asins]
            with open(BLACKLIST_FILE, "a", encoding="utf-8") as f:
                for item in new_to_blacklist:
                    f.write(f"{item['asin']}  # {item['title'][:70]}\n")

            known_products = load_asin_set(products_file)
            new_to_products = [item for item in scraped if item["asin"] not in known_products]
            with open(products_file, "a", encoding="utf-8") as f:
                for item in new_to_products:
                    f.write(f"{item['asin']}  # {item['title'][:70]}\n")

            baseline_marker.touch()
            logger.info(
                f"[{market}] Baseline scan complete — added {len(new_to_blacklist)} new ASIN(s) to the "
                f"shared blacklist ({len(scraped) - len(new_to_blacklist)} already recorded there — from "
                f"another market's baseline, or deliberately un-blacklisted and left alone) and "
                f"{len(new_to_products)} to this market's products.txt. No notifications on this baseline pass."
            )
            continue

        # "Already known" = blacklisted (ignored everywhere) OR already
        # tracked in this market's products.txt. We deliberately do NOT
        # blacklist new arrivals after flagging them (see module
        # docstring) — since track_delivery_multi.py now excludes
        # blacklisted ASINs from delivery tracking entirely, blacklisting
        # a new arrival here would immediately stop it from being tracked,
        # defeating the point of adding it to products.txt in the first
        # place. products.txt membership alone is enough to prevent
        # re-notifying about it next run.
        already_known = blacklisted_asins | load_asin_set(products_file)
        new_items = [item for item in scraped if item["asin"] not in already_known]

        if not new_items:
            logger.info(f"[{market}] No new arrivals on page 1.")
            continue

        logger.info(f"[{market}] Found {len(new_items)} new arrival(s):")
        with open(products_file, "a", encoding="utf-8") as pf:
            for item in new_items:
                logger.info(f"  {item['asin']} — {item['title'][:70]}")
                pf.write(f"{item['asin']}  # {item['title'][:70]}\n")

        lines = "\n".join(f"• {item['title'][:70]} ({item['asin']})" for item in new_items)
        notify(
            f"🆕 [{market.upper()}] {len(new_items)} new Hasbro Beyblade X arrival(s):\n{lines}",
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
