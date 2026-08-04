#!/usr/bin/env python3
"""
LOCAL PILOT — EU multi-marketplace "new product" watcher.

For each market: search Beyblade X filtered to the Hasbro brand, sorted
newest-first, read PAGE ONE ONLY, and compare what's there against that
market's own list of products it has seen before. Anything not on the
list is a new arrival — ping Discord, then add it to the list so it
never pings again.

Page one is enough precisely because the sort is date-desc: a genuinely
new listing appears at the top. Paginating would only re-read older
products we already know about.

**Per-market lists are deliberate.** The same ASIN turning up on
amazon.fr months after amazon.se is new information — it means the
product just became available in a market that didn't have it — so it
should ping again there. state/<market>/products.txt is that market's
list.

**First run seeds silently.** With an empty list, every result on page
one looks new, which would be dozens of Discord messages saying nothing
useful. So a market with no known products records what it finds without
notifying; from the second run on, anything new is genuinely new. The
seeded list is written to the log so you can see what it decided the
baseline was.

Does NOT touch state/whitelist.txt — the delivery tracker's list stays
hand-maintained, so a discovery here is a suggestion to you, not an
automatic addition to what gets delivery-tracked.

Usage:
    python pilot/eu_multimarket/scrape_catalog_multi.py [market ...]
    python pilot/eu_multimarket/scrape_catalog_multi.py --send-discord

Env vars:
    HEADLESS           "true" (default) or "false" to watch the browser
    DELIVERY_COUNTRY   destination country (default Sweden)
    DELIVERY_POSTCODE  destination postcode, domestic market only
    DISCORD_WEBHOOK_URL / DISCORD_USER_ID
    DEBUG              "true" to dump the search HTML for every market
"""

import argparse
import os
import sys
import time
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
import browser
from browser import load_asin_set, notify, open_market, safe_goto
from logging_setup import setup_logger
from marketplaces import MARKETPLACES

PILOT_DIR = Path(__file__).parent
DEBUG = os.environ.get("DEBUG", "").lower() == "true"

# Markets checked when none are named. Same default as the delivery
# tracker; nl/be/it/pl are configured and can be named explicitly.
DEFAULT_MARKETS = ["se", "de", "fr", "es"]

# Supplied verbatim by the user, and the sort order is the whole point:
#   k=beyblade x         the search term
#   rh=p_123:219753      brand filter (Hasbro)
#   s=date-desc-rank     NEWEST ARRIVALS FIRST — this is what makes
#                        reading only page one sufficient
#   language=en          English results, matching the pilot's "/-/en/"
#                        convention so titles are parseable
SEARCH_PATH = "/s?k=beyblade+x&rh=p_123%3A219753&s=date-desc-rank&dc&language=en"

# The canonical search-result tile. Amazon puts a lot of other things in
# div[data-asin] (carousels, ad slots, layout scaffolding), so anchor on
# the component type rather than just the attribute.
RESULT_SELECTOR = 'div[data-component-type="s-search-result"][data-asin]'

# Sponsored tiles are injected into the results and are NOT date-sorted —
# they're ads, frequently for other brands entirely. Counting one as a
# new arrival would be a false alert, and because the list is written
# back, it would also permanently poison that market's baseline.
SPONSORED_MARKERS = [
    ".puis-sponsored-label-text",
    '[data-component-type="sp-sponsored-result"]',
    ".s-sponsored-label-text",
]

# A new listing worth alerting on always has a real ASIN; Amazon uses
# placeholder/empty data-asin values for layout rows.
ASIN_LENGTH = 10

MAX_DISCORD_MESSAGES = 10

logger = setup_logger("catalog_multi")
browser.use_logger(logger)


def products_file(market: str) -> Path:
    return PILOT_DIR / "state" / market / "products.txt"


def is_sponsored(card) -> bool:
    if any(card.select_one(marker) for marker in SPONSORED_MARKERS):
        return True
    # Belt and braces: some layouts render the label as plain text in an
    # otherwise unremarkable span.
    return "sponsored" in card.get_text(" ", strip=True)[:120].lower()


def parse_results(html: str, market: str) -> list[dict]:
    """Extract (asin, title, price) from page one of the search results."""
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(RESULT_SELECTOR)
    logger.info(f"[{market}]   {len(cards)} search-result tile(s) on page 1")

    products = []
    sponsored = 0
    for card in cards:
        asin = (card.get("data-asin") or "").strip()
        if len(asin) != ASIN_LENGTH:
            continue
        if is_sponsored(card):
            sponsored += 1
            continue
        title_el = card.select_one("h2")
        price_el = card.select_one(".a-price .a-offscreen")
        products.append(
            {
                "asin": asin,
                "title": title_el.get_text(" ", strip=True) if title_el else "(no title found)",
                "price": price_el.get_text(strip=True) if price_el else "",
            }
        )

    if sponsored:
        logger.info(f"[{market}]   skipped {sponsored} sponsored tile(s)")
    # Amazon can repeat an ASIN across tiles; keep first-seen order,
    # which under date-desc means newest first.
    seen, unique = set(), []
    for product in products:
        if product["asin"] not in seen:
            seen.add(product["asin"])
            unique.append(product)
    logger.info(f"[{market}]   {len(unique)} distinct organic product(s)")
    return unique


def append_products(path: Path, products: list[dict]) -> None:
    """Append to the market's list in the repo's usual 'ASIN  # Name' form."""
    path.parent.mkdir(parents=True, exist_ok=True)
    header = ""
    if not path.exists():
        header = (
            "# Products this market has been seen to carry, one ASIN per line.\n"
            "# Written by scrape_catalog_multi.py — an ASIN here has already\n"
            "# been notified about and won't ping again on this market.\n"
        )
    with path.open("a", encoding="utf-8") as handle:
        if header:
            handle.write(header)
        for product in products:
            handle.write(f"{product['asin']}  # {product['title'][:90]}\n")


def check_market(market: str, send_discord: bool) -> dict:
    config = MARKETPLACES[market]
    path = products_file(market)
    known = load_asin_set(path)
    logger.info(f"=== [{market}] {len(known)} product(s) already known ===")

    url = f"https://www.{config['domain']}{SEARCH_PATH}"
    with sync_playwright() as p:
        browser_handle, page, _location = open_market(p, market, config)
        try:
            logger.info(f"[{market}] searching: {url}")
            safe_goto(page, url, market)
            page.wait_for_timeout(1500)
            html = page.content()
        finally:
            logger.info(f"[{market}] closing browser")
            browser_handle.close()

    products = parse_results(html, market)

    # Zero results is never a normal answer for a brand-filtered search
    # that is known to have a catalogue. Most likely causes: the brand
    # filter id doesn't mean Hasbro on this marketplace (they're not
    # guaranteed to be shared), or we got served a bot check. Either way
    # it must not read as "no new products".
    if not products:
        debug_file = PILOT_DIR / "state" / market / "debug_search.html"
        debug_file.parent.mkdir(parents=True, exist_ok=True)
        debug_file.write_text(html, encoding="utf-8")
        logger.warning(
            f"[{market}] NO SEARCH RESULTS AT ALL — this is not 'no new products'. "
            f"Check whether the brand filter in SEARCH_PATH means Hasbro on "
            f"{config['domain']}, or whether we were served a bot check. "
            f"Saved {debug_file}"
        )
        return {"found": 0, "new": 0, "seeded": 0}

    if DEBUG:
        debug_file = PILOT_DIR / "state" / market / "debug_search.html"
        debug_file.parent.mkdir(parents=True, exist_ok=True)
        debug_file.write_text(html, encoding="utf-8")
        logger.info(f"[{market}] --debug: saved {debug_file}")

    new = [product for product in products if product["asin"] not in known]

    # First run for this market: record the baseline without notifying.
    if not known:
        logger.info(
            f"[{market}] no existing list — seeding {len(new)} product(s) silently. "
            f"From the next run on, anything new here is genuinely new:"
        )
        for product in new:
            logger.info(f"[{market}]     {product['asin']}  {product['title'][:80]}")
        append_products(path, new)
        return {"found": len(products), "new": 0, "seeded": len(new)}

    if not new:
        logger.info(f"[{market}] no new products")
        return {"found": len(products), "new": 0, "seeded": 0}

    logger.info(f"[{market}] {len(new)} NEW product(s)")
    for i, product in enumerate(new):
        price = f"  {product['price']}" if product["price"] else ""
        message = (
            f"🆕 [{market.upper()}] New Beyblade X product on {config['domain']}:\n"
            f"  **{product['title'][:150]}**{price}\n"
            f"  https://www.{config['domain']}/dp/{product['asin']}"
        )
        if i < MAX_DISCORD_MESSAGES:
            notify(message, send_discord)
        else:
            logger.info(message)
    if len(new) > MAX_DISCORD_MESSAGES:
        notify(
            f"…and {len(new) - MAX_DISCORD_MESSAGES} more new product(s) on "
            f"{config['domain']} — see the run log.",
            send_discord,
        )

    # Written only after notifying, so a crash mid-run means you get the
    # alert again next time rather than losing it silently.
    append_products(path, new)
    return {"found": len(products), "new": len(new), "seeded": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "markets",
        nargs="*",
        default=DEFAULT_MARKETS,
        help=f"Market codes to check (default: {' '.join(DEFAULT_MARKETS)}; "
             f"all configured: {' '.join(MARKETPLACES)})",
    )
    parser.add_argument(
        "--send-discord",
        action="store_true",
        help="Actually post to Discord instead of just logging what would be sent",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=DEBUG,
        help="Save the search HTML for every market, not just on zero results",
    )
    args = parser.parse_args()
    DEBUG = args.debug

    logger.info(f"=== START catalog_multi markets={args.markets} debug={DEBUG} ===")
    try:
        totals = {}
        for market in args.markets:
            if market not in MARKETPLACES:
                logger.error(f"Unknown market '{market}' — choices are {list(MARKETPLACES)}")
                continue
            start = time.monotonic()
            totals[market] = check_market(market, args.send_discord)
            logger.info(f"[{market}] done in {time.monotonic() - start:.1f}s")

        logger.info("=== NEW PRODUCTS BY MARKET ===")
        for market, counts in totals.items():
            seeded = f"  (seeded {counts['seeded']})" if counts["seeded"] else ""
            logger.info(
                f"  {market:<3} {counts['new']} new of {counts['found']} found{seeded}"
            )
        logger.info("=== END catalog_multi (exit 0) ===")
    except Exception:
        logger.exception("=== END catalog_multi (exit 1) ===")
        sys.exit(1)
