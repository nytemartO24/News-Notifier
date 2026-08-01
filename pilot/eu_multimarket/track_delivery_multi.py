#!/usr/bin/env python3
"""
LOCAL PILOT — EU multi-marketplace delivery-date tracker.

No catalog scraping/discovery at all — this is a pure whitelist-based
delivery-date checker. You maintain state/whitelist.txt by hand (one
ASIN per line); every ASIN in it gets checked directly
(https://www.<domain>/dp/<asin>, no search) on every market you name on
the command line. There is no blacklist, no "new arrival" notification,
no per-market product list — just a flat list of specific products you
told it to watch, checked everywhere. Comment out a whitelist line
(leading '#') to pause tracking that ASIN without losing it; delete the
line to drop it for good.

Extends scripts/track_delivery_date_playwright.py with per-marketplace
locale handling (month names, "no date"/"out of stock" phrasing — see
marketplaces.py). Standalone pilot: reads/writes only under
pilot/eu_multimarket/state/ — does NOT touch scripts/delivery_state.json
or any production state.

NO_DATE_SIGNALS / OUT_OF_STOCK_SIGNALS for every market except "se" are
unverified guesses (see marketplaces.py) — expect "UNKNOWN" results and
debug_*.html dumps under state/<market>/ until those are tuned from real
output.

Logs every step per product (navigation, interstitial handling, which
selector/signal matched, elapsed time) to logs/delivery_multi.log and the
console — if a run looks stalled, tail that log; the timestamp on the
last line pinpoints exactly which step it's stuck on rather than which
product.

Usage:
    python pilot/eu_multimarket/track_delivery_multi.py [market ...]
    # reads ASINs from state/whitelist.txt (create/edit it by hand) and
    # checks that same list against every named market (default: all
    # configured markets)

Env vars:
    HEADLESS   "true" (default) or "false" — set false to watch the
               browser locally while debugging a stuck/slow market
"""

import argparse
import datetime
import json
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
WHITELIST_FILE = PILOT_DIR / "state" / "whitelist.txt"  # hand-maintained, shared across all markets
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
DISCORD_USER_ID = os.environ.get("DISCORD_USER_ID", "")
HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"

CANDIDATE_SELECTORS = [
    "#mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE",
    "#deliveryBlockMessage",
    "#contextualIngressPtLabel_deliveryShortDeliveryDate",
    "#deliveryMessageMirId",
    "#mir-layout-DELIVERY_BLOCK",
]

# UNVERIFIED — best-guess buybox selectors for "Dispatches from" / "Sold
# by" info, not confirmed against real pages on any market here. Amazon's
# buybox layout varies (classic #merchant-info vs newer tabular-buybox);
# tune against real output the same way the delivery-date selectors were.
SELLER_SELECTORS = [
    "#merchant-info",
    "#tabular-buybox",
    "#aod-offer-soldBy",
    "#usedBuyBoxOOS",
]

# UNVERIFIED — best-guess "sold by <seller>" phrasing per locale, used to
# find the SPECIFIC seller name rather than just checking whether "amazon"
# appears anywhere in the block. That distinction matters: a third-party
# listing dispatched by Amazon but sold by someone else often still reads
# "Dispatches from Amazon / Sold by Skydigital" — checking for "amazon"
# anywhere in that block would wrongly call it Amazon-sold.
SOLD_BY_PATTERNS = [
    r"sold by\s*[:\-]?\s*([^.\n]+)",           # en
    r"verkauft von\s*[:\-]?\s*([^.\n]+)",       # de
    r"vendu(?:e)? par\s*[:\-]?\s*([^.\n]+)",     # fr
    r"vendido por\s*[:\-]?\s*([^.\n]+)",          # es
    r"venduto da\s*[:\-]?\s*([^.\n]+)",            # it
    r"verkocht door\s*[:\-]?\s*([^.\n]+)",          # nl / be
    r"sprzedawca[:\-]?\s*([^.\n]+)",                 # pl
]

MAX_NAME_LENGTH = 40

logger = setup_logger("delivery_multi")


def build_date_pattern(months: dict) -> re.Pattern:
    names = "|".join(sorted(months.keys(), key=len, reverse=True))
    return re.compile(rf"\d{{1,2}}\.?\s+(?:{names})\.?,?\s*(?:\d{{4}})?", re.IGNORECASE)


def parse_date_for_sorting(date_str: str, months: dict):
    m = re.search(r"(\d{1,2})\.?\s+([^\s\d]+)\.?,?\s*(\d{4})?", date_str)
    if not m:
        return None
    day, month_name, year = m.groups()
    month = months.get(month_name.lower())
    if not month:
        return None
    today = datetime.date.today()
    year = int(year) if year else today.year
    try:
        parsed = datetime.date(year, month, int(day))
    except ValueError:
        return None
    if not m.group(3) and parsed < today:
        parsed = parsed.replace(year=year + 1)
    return parsed


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
    # A fully-commented line (leading '#') contributes no ASIN — that's
    # how you pause tracking an item without deleting it.
    if not path.exists():
        return set()
    result = set()
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        asin = line.strip().split("#")[0].strip()
        if asin:
            result.add(asin)
    return result


def load_whitelist() -> list[str]:
    if not WHITELIST_FILE.exists():
        WHITELIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        WHITELIST_FILE.write_text(
            "# One ASIN per line — every one gets checked on every market you\n"
            "# run this script against. Comment out a line ('#' at the very\n"
            "# start) to pause tracking it without losing it; delete the line\n"
            "# to drop it for good.\n"
            "#\n"
            "# Example:\n"
            "# B0G4NF8QDZ  # Beyblade X Curse Mummy 7-55W UX Booster Pack\n"
        )
        logger.warning(f"Created {WHITELIST_FILE} — add ASINs and rerun.")
        return []

    asins = sorted(load_asin_set(WHITELIST_FILE))
    logger.info(f"{len(asins)} product(s) in whitelist.")
    return asins


def dismiss_continue_shopping_interstitial(page, market: str) -> bool:
    try:
        if page.locator("form[action='/errors_page/validateCaptcha']").count() == 0:
            return False
        logger.info(f"[{market}]     interstitial detected — clicking through")
        button = page.locator("form[action='/errors_page/validateCaptcha'] button[type='submit']")
        if button.count() == 0:
            logger.info(f"[{market}]     interstitial had no submit button, giving up on it")
            return False
        button.first.click()
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        page.wait_for_timeout(1500)
        logger.info(f"[{market}]     interstitial dismissed")
        return True
    except Exception as e:
        logger.warning(f"[{market}]     interstitial dismissal failed: {e}")
        return False


def fetch_seller_info(soup) -> str:
    for selector in SELLER_SELECTORS:
        el = soup.select_one(selector)
        if el and el.get_text(strip=True):
            return el.get_text(" ", strip=True)
    return ""


def is_amazon_seller(seller_text: str):
    """True/False/None (unknown, no seller block found at all).
    Looks specifically for the "sold by <name>" portion via
    SOLD_BY_PATTERNS and checks whether THAT name mentions Amazon — not
    whether "amazon" appears anywhere in the block, since a third-party
    listing dispatched by Amazon often still says "Dispatches from
    Amazon / Sold by Skydigital", which mentions Amazon without being
    Amazon-sold. Falls back to a whole-block check only if no "sold by"
    label in any known language is found. UNVERIFIED against real pages
    — tune this (and SELLER_SELECTORS/SOLD_BY_PATTERNS) if it starts
    misclassifying."""
    if not seller_text:
        return None
    text_lower = seller_text.lower()
    for pattern in SOLD_BY_PATTERNS:
        m = re.search(pattern, text_lower, re.IGNORECASE)
        if m:
            return "amazon" in m.group(1)
    return "amazon" in text_lower


def fetch_delivery_date(page, url: str, market: str, config: dict, date_pattern: re.Pattern) -> dict:
    logger.info(f"[{market}]   navigating to {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    logger.info(f"[{market}]   page loaded (domcontentloaded), settling...")
    page.wait_for_timeout(2000)

    for attempt in range(2):
        if not dismiss_continue_shopping_interstitial(page, market):
            break
        logger.info(f"[{market}]   re-checking for a chained interstitial (attempt {attempt + 1}/2)")

    logger.info(f"[{market}]   reading page content")
    html = page.content()
    soup = BeautifulSoup(html, "html.parser")

    seller_text = fetch_seller_info(soup)
    amazon_seller = is_amazon_seller(seller_text)
    if seller_text:
        logger.info(f"[{market}]   seller/dispatch block: {seller_text!r} (amazon={amazon_seller})")
    else:
        logger.info(f"[{market}]   no seller/dispatch block found (SELLER_SELECTORS unverified — see comment)")

    def result(date: str) -> dict:
        return {"date": date, "seller_text": seller_text, "is_amazon_seller": amazon_seller}

    text_blob = ""
    matched_selector = None
    for selector in CANDIDATE_SELECTORS:
        el = soup.select_one(selector)
        if el and el.get_text(strip=True):
            text_blob = el.get_text(" ", strip=True)
            matched_selector = selector
            break

    if matched_selector:
        logger.info(f"[{market}]   delivery text found via selector {matched_selector!r}")
        match = date_pattern.search(text_blob)
        if match:
            date_str = re.sub(r"\s+", " ", match.group()).strip().rstrip(",")
            logger.info(f"[{market}]   date pattern matched: {date_str!r}")
            return result(date_str)
    else:
        # No known delivery-block selector matched. Deliberately do NOT
        # run the date pattern against the full page text here — it's too
        # promiscuous, matching unrelated dates anywhere on the page
        # (review timestamps, editorial content, etc). A "21 January
        # 2026" found this way could just as easily be a review's
        # "Reviewed in Spain on 21 January 2026" as an actual delivery
        # estimate, and since it carries an explicit year, the
        # this-date-already-passed-so-assume-next-year correction in
        # parse_date_for_sorting() never kicks in for it — a stale,
        # unrelated date would sail through looking like a real one.
        logger.info(f"[{market}]   no delivery-block selector matched — skipping date-pattern search entirely")

    logger.info(f"[{market}]   checking out-of-stock/no-date signal phrases")
    full_text_lower = soup.get_text(" ", strip=True).lower()

    for signal in config["out_of_stock_signals"]:
        if signal in full_text_lower:
            logger.info(f"[{market}]   matched out-of-stock signal: {signal!r}")
            return result("OUT OF STOCK (temporarily unavailable, no delivery date yet)")

    for signal in config["no_date_signals"]:
        if signal in full_text_lower:
            logger.info(f"[{market}]   matched no-date signal: {signal!r}")
            return result("NO DATE YET (listing has no confirmed delivery estimate)")

    debug_dir = PILOT_DIR / "state" / market
    debug_dir.mkdir(parents=True, exist_ok=True)
    debug_file = debug_dir / f"debug_{url.rsplit('/', 1)[-1]}.html"
    debug_file.write_text(html, encoding="utf-8")
    logger.info(f"[{market}]   no match at all — saved {debug_file}")
    return result("UNKNOWN — no date found, saved debug HTML")


def get_product_title(page) -> str:
    title = page.title()
    return title.split(" :")[0].strip() if title else "Unknown product"


def truncate_name(name: str) -> str:
    return name if len(name) <= MAX_NAME_LENGTH else name[: MAX_NAME_LENGTH - 1].rstrip() + "…"


def check_market(market: str, asins: list[str], send_discord: bool) -> None:
    config = MARKETPLACES[market]
    market_dir = PILOT_DIR / "state" / market
    market_dir.mkdir(parents=True, exist_ok=True)
    state_file = market_dir / "delivery_state.json"

    if not asins:
        return

    logger.info(f"=== [{market}] Checking {len(asins)} product(s) ===")

    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    date_pattern = build_date_pattern(config["months"])

    # A real delivery estimate can never legitimately be in the past — so
    # any stored date that's already passed is evidence of past
    # corruption (e.g. the full-page-text date-regex false positives this
    # replaced), not a value worth protecting via "keep previous state on
    # UNKNOWN." Discard it so a genuinely unavailable date reads as
    # missing rather than as a stale, misleading one.
    today = datetime.date.today()
    for asin in list(state.keys()):
        parsed = parse_date_for_sorting(state[asin].get("date", ""), config["months"])
        if parsed is not None and parsed < today:
            logger.warning(f"[{market}] discarding stale state for {asin}: {state[asin]['date']!r} is in the past")
            del state[asin]

    with sync_playwright() as p:
        logger.info(f"[{market}] launching Chromium (headless={HEADLESS})")
        browser = p.chromium.launch(headless=HEADLESS, channel="chromium")
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale=config["locale"],
        )
        page = context.new_page()

        logger.info(f"[{market}] warming up: navigating to https://www.{config['domain']}/")
        try:
            page.goto(f"https://www.{config['domain']}/", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)
            dismiss_continue_shopping_interstitial(page, market)
            logger.info(f"[{market}] warm-up complete")
        except Exception as e:
            logger.warning(f"[{market}] homepage warm-up failed: {e}")

        for i, asin in enumerate(asins, start=1):
            url = f"https://www.{config['domain']}/dp/{asin}"
            logger.info(f"[{market}] ({i}/{len(asins)}) checking {asin}")
            start = time.monotonic()
            try:
                result = fetch_delivery_date(page, url, market, config, date_pattern)
                name = get_product_title(page)
            except Exception as e:
                logger.warning(f"[{market}] ({i}/{len(asins)}) {asin} failed after {time.monotonic() - start:.1f}s: {e}")
                continue

            current_date = result["date"]
            seller_text = result["seller_text"]
            is_amazon = result["is_amazon_seller"]
            elapsed = time.monotonic() - start
            seller_note = "amazon" if is_amazon else ("third-party" if is_amazon is False else "seller unknown")
            logger.info(f"[{market}] ({i}/{len(asins)}) {asin} = {current_date!r} [{seller_note}] ({elapsed:.1f}s)")
            if is_amazon is False:
                logger.warning(f"[{market}] {asin}: NOT sold by Amazon (seller: {seller_text!r}) — price/date may not be Amazon's own")

            if current_date.startswith("UNKNOWN"):
                logger.warning(f"[{market}] {asin}: {current_date} — keeping previous state")
                continue

            previous_date = state.get(asin, {}).get("date")
            if previous_date is not None and current_date != previous_date:
                old_parsed = parse_date_for_sorting(previous_date, config["months"])
                new_parsed = parse_date_for_sorting(current_date, config["months"])
                became_earlier_or_new = new_parsed is not None and (
                    old_parsed is None or new_parsed < old_parsed
                )
                if became_earlier_or_new:
                    seller_line = (
                        f"  seller: {seller_text}"
                        + (" ⚠️ NOT sold by Amazon" if is_amazon is False else "")
                        + "\n"
                        if seller_text
                        else ""
                    )
                    notify(
                        f"📦 [{market.upper()}] Delivery date moved earlier for **{name}**:\n"
                        f"  was: {previous_date}\n"
                        f"  now: {current_date}\n"
                        f"{seller_line}"
                        f"  {url}",
                        send_discord,
                    )

            state[asin] = {
                "name": name,
                "date": current_date,
                "seller_text": seller_text,
                "is_amazon_seller": is_amazon,
            }

        logger.info(f"[{market}] closing browser")
        browser.close()

    state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False))

    rows = [
        (
            parse_date_for_sorting(info["date"], config["months"]),
            truncate_name(info["name"]),
            info["date"],
            info.get("is_amazon_seller"),
        )
        for asin, info in state.items()
        if asin in asins
    ]
    rows.sort(key=lambda r: (r[0] is None, r[0]))

    name_width = max((len(name) for _, name, _, _ in rows), default=len("Product"))
    separator = "-" * (name_width + 3 + 40)
    logger.info(f"DELIVERY SUMMARY [{market.upper()}] — {time.strftime('%Y-%m-%d %H:%M:%S')}")
    for _, name, date_str, is_amazon in rows:
        flag = " ⚠️ NOT AMAZON" if is_amazon is False else ("" if is_amazon else " (seller unknown)")
        logger.info(f"  {name:<{name_width}} | {date_str}{flag}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("markets", nargs="*", default=list(MARKETPLACES), help="Market codes to check (default: all configured)")
    parser.add_argument("--send-discord", action="store_true", help="Actually post to Discord instead of just logging what would be sent")
    args = parser.parse_args()

    logger.info(f"=== START delivery_multi markets={args.markets} ===")
    try:
        tracked_asins = load_whitelist()
        if not tracked_asins:
            logger.info("Nothing to track — exiting.")
        else:
            for market in args.markets:
                if market not in MARKETPLACES:
                    logger.error(f"Unknown market '{market}' — choices are {list(MARKETPLACES)}")
                    continue
                check_market(market, tracked_asins, args.send_discord)
        logger.info("=== END delivery_multi (exit 0) ===")
    except Exception:
        logger.exception("=== END delivery_multi (exit 1) ===")
        sys.exit(1)
