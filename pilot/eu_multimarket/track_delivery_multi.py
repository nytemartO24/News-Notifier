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
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

sys.path.insert(0, str(Path(__file__).parent))
from logging_setup import setup_logger
from marketplaces import ENGLISH_MONTHS, MARKETPLACES

PILOT_DIR = Path(__file__).parent
WHITELIST_FILE = PILOT_DIR / "state" / "whitelist.txt"  # hand-maintained, shared across all markets
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
DISCORD_USER_ID = os.environ.get("DISCORD_USER_ID", "")
HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"
# Verbose per-product page diagnostics. Set by --debug or DEBUG=true.
# Reassigned in __main__ once the CLI flag is parsed.
DEBUG = os.environ.get("DEBUG", "").lower() == "true"

# Where we're pretending to be, for delivery-estimate purposes.
#
# This is not cosmetic. A delivery date only means anything relative to a
# destination, and Amazon picks one by geolocation if you don't tell it —
# which produced a genuinely useless run on 2026-08-04: se/de/nl/be/pl
# each resolved to a local address (Stockholm, Nuremberg, Amsterdam,
# Brussels, Warsaw) while fr/es/it all resolved to "Deliver to Germany"
# (the VPS's own country) and served offer-less cross-border pages. So no
# two markets were answering the same question, and only .se was
# answering the one that matters: "when would this arrive at MY address".
#
# Pinning one destination across every market makes the numbers
# comparable and makes "is it cheaper/sooner from .de than .se?" a
# question the pilot can actually answer.
# Only the domestic market can take a postcode (amazon.se's modal has no
# country picker at all — see select_country); everywhere else Amazon
# quotes country-level estimates only. Written without the space that
# Swedish postcodes normally carry ("371 16"); fill_postcode() splits it
# across however many input fields the market's modal uses.
DELIVERY_COUNTRY = os.environ.get("DELIVERY_COUNTRY", "Sweden")
DELIVERY_POSTCODE = os.environ.get("DELIVERY_POSTCODE", "37116")

# Amazon's "Deliver to ..." location widget ("glow").
GLOW_INGRESS_SELECTOR = "#glow-ingress-line2"
GLOW_OPENER_SELECTORS = [
    "#nav-global-location-popover-link",
    "#glow-ingress-block",
    GLOW_INGRESS_SELECTOR,
]

# Markets checked when none are named on the command line. The other
# four (nl, be, it, pl) stay fully configured in marketplaces.py and can
# still be run explicitly — they're deliberately not deleted, since
# their month/signal tables are tested and pl's genitive months and
# confirmed "obecnie niedostępny" phrase would be tedious to rebuild.
DEFAULT_MARKETS = ["se", "de", "fr", "es"]

CANDIDATE_SELECTORS = [
    "#mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE",
    "#deliveryBlockMessage",
    "#contextualIngressPtLabel_deliveryShortDeliveryDate",
    "#deliveryMessageMirId",
    "#mir-layout-DELIVERY_BLOCK",
    # Layout-independent fallbacks. The first is the attribute Amazon
    # tags its unified delivery-promise container with, which survives
    # the element-id churn that differs between markets/experiments; the
    # rest are older ids still seen on some layouts. All are
    # delivery-specific, so widening here cannot pull in a review date
    # or an editorial date the way a full-page regex search would.
    '[data-csa-c-content-id="DEXUnifiedCXPDM"]',
    "#ddmDeliveryMessage",
    "#fast-track-message",
]

# Signal phrases are matched ONLY inside these, never against the whole
# page. Whole-page matching is a false-positive machine: an Amazon toy
# listing's product-details table almost always contains a "Release
# date" row, so a page whose delivery block simply hadn't rendered would
# get confidently reported as "NO DATE YET" rather than the honest
# UNKNOWN. These containers hold availability/buybox copy only — the
# details table, reviews and related-items carousels are all outside
# them.
AVAILABILITY_SELECTORS = [
    "#availability",
    "#outOfStock",
    "#exports_desktop_outOfStock_buybox_feature_div",
    "#desktop_buybox",
    "#qualifiedBuybox",
    "#buybox",
]

# A delivery estimate is never in the past, and Amazon does not quote
# arrivals years out. Anything outside this window is a parse artefact
# (a stray "2019" in marketing copy, a mis-anchored day number), so it
# is rejected rather than stored and alerted on.
MAX_DELIVERY_HORIZON_DAYS = 400

# ---------------------------------------------------------------------------
# Diagnostics
#
# These exist because the GitHub Actions job log is the only reliable
# way to see what a run actually saw: the state/ artifact is hosted on
# Azure Blob Storage, which the restrictive proxies in sandboxed dev
# environments tend to block, so "download the debug HTML and look at
# it" often isn't available. Everything you'd want out of that HTML is
# therefore summarised into the log itself.
#
# Nothing below ever feeds a stored value or an alert — it is strictly
# reporting. The whole-page scans in particular are DIAGNOSTIC ONLY;
# they are exactly the promiscuous searches the extraction path
# deliberately refuses to do (see the comments there), and printing what
# they'd have found is safe precisely because nothing acts on it.
# ---------------------------------------------------------------------------

# Anchors that identify what KIND of page we actually landed on. The
# difference between "product page, delivery block absent" and "not a
# product page at all" is the single most useful thing to know when a
# market returns nothing, and it is invisible from the result alone.
PAGE_KIND_SELECTORS = {
    "product title": "#productTitle",
    "detail page root": "#dp, #ppd",
    "centre column": "#centerCol",
    "add-to-cart button": "#add-to-cart-button",
    "buy-now button": "#buy-now-button",
    "price block": "#corePrice_feature_div, #priceblock_ourprice",
    "CAPTCHA input": "#captchacharacters",
    "cookie banner": "#sp-cc",
    # A date is meaningless without knowing where it's being delivered
    # to, so every snapshot carries the destination Amazon used.
    "delivering to": "#glow-ingress-line2",
}

# Delivery copy in every pilot language, English first. Used only to
# locate and print the page's actual delivery wording when our selectors
# found nothing — that wording is what you need in order to fix either a
# selector or a phrase list.
DELIVERY_KEYWORDS = [
    "delivery", "arrives", "dispatch", "shipping", "order within",
    "leverans", "lieferung", "livraison", "entrega", "consegna",
    "dostawa", "bezorging", "verzending",
]

MAX_DIAGNOSTIC_SNIPPETS = 8
SNIPPET_CONTEXT_CHARS = 70


def log_run_config(markets: list[str]) -> None:
    """Print the config each market will actually run with.

    Worth the log lines: the pilot's longest-standing bug was a config
    one (native-only month tables against English pages), and it was
    invisible in the output — every market just said UNKNOWN. Printing
    the resolved tables makes that class of failure legible from the job
    log alone.
    """
    logger.info("=== resolved marketplace config ===")
    for market in markets:
        config = MARKETPLACES.get(market)
        if not config:
            continue
        months = config["months"]
        english = sum(1 for name in months if name in ENGLISH_MONTHS)
        logger.info(
            f"  [{market}] domain={config['domain']} locale={config['locale']} "
            f"months={len(months)} ({english} English, {len(months) - english} native) "
            f"no_date_signals={len(config['no_date_signals'])} "
            f"out_of_stock_signals={len(config['out_of_stock_signals'])}"
        )
        if DEBUG:
            logger.info(f"    months:          {sorted(months)}")
            logger.info(f"    no_date:         {config['no_date_signals']}")
            logger.info(f"    out_of_stock:    {config['out_of_stock_signals']}")


def _clip(text: str, limit: int = 300) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit] + f"… (+{len(text) - limit} chars)"


def describe_selector(soup, selector: str) -> str:
    """Distinguish 'element absent' from 'element present but empty' —
    they mean different things. Absent points at a selector that's wrong
    for this market's layout; present-but-empty points at the
    client-side-injection problem, i.e. the container rendered but its
    contents hadn't been filled in when we read the page."""
    elements = soup.select(selector)
    if not elements:
        return "ABSENT"
    parts = []
    for el in elements:
        text = el.get_text(" ", strip=True)
        # <input>/<button> carry their label in an attribute, not as
        # child text — without this they'd all read "PRESENT BUT EMPTY"
        # and look like the injection problem when they're perfectly fine.
        if not text and el.name in ("input", "button"):
            text = el.get("value") or el.get("aria-label") or ""
        if text:
            parts.append(text)
    if not parts:
        return f"PRESENT BUT EMPTY ({len(elements)} node(s)) — container rendered, contents not filled in"
    return f"PRESENT: {_clip(' '.join(parts), 200)!r}"


def log_page_snapshot(market: str, asin: str, page, soup, html: str, log) -> None:
    """What the scraper is looking at, before any interpretation."""
    html_tag = soup.find("html")
    lang = html_tag.get("lang") if html_tag else None

    log(f"[{market}] --- page snapshot for {asin} ---")
    log(f"[{market}]   final URL:    {page.url}")
    log(f"[{market}]   <title>:      {_clip(page.title() or '', 160)!r}")
    # The whole point of the "/-/en/" override is that this reads "en".
    # If it says "de"/"fr"/etc, the override lost and the native tables
    # in marketplaces.py are the ones doing the work.
    log(f"[{market}]   <html lang>:  {lang!r}  (expect an 'en' variant — '/-/en/' URL override)")
    log(f"[{market}]   HTML size:    {len(html):,} bytes")

    log(f"[{market}]   page kind:")
    for label, selector in PAGE_KIND_SELECTORS.items():
        log(f"[{market}]     {label:<20} {describe_selector(soup, selector)}")

    log(f"[{market}]   delivery-block selectors:")
    for selector in CANDIDATE_SELECTORS:
        log(f"[{market}]     {selector:<62} {describe_selector(soup, selector)}")

    log(f"[{market}]   availability/buybox selectors (signal-match scope):")
    for selector in AVAILABILITY_SELECTORS:
        log(f"[{market}]     {selector:<62} {describe_selector(soup, selector)}")

    log(f"[{market}]   seller selectors:")
    for selector in [SELLER_CONTAINER_SELECTOR] + SELLER_FALLBACK_SELECTORS:
        log(f"[{market}]     {selector:<62} {describe_selector(soup, selector)}")


def log_text_probes(market: str, soup, date_pattern: re.Pattern, config: dict, log) -> None:
    """DIAGNOSTIC ONLY — whole-page scans whose results are printed and
    then thrown away. Answers the two questions you actually have when a
    market returns nothing: 'is there a date anywhere on this page that
    we failed to reach?' and 'what does this page's delivery copy
    literally say?'"""
    page_text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    log(f"[{market}]   visible text: {len(page_text):,} chars")

    matches = list(date_pattern.finditer(page_text))
    if matches:
        log(
            f"[{market}]   [diagnostic] {len(matches)} date-like string(s) anywhere on the page "
            f"(NOT used — shown so you can see what the parser could reach if a selector matched):"
        )
        for match in matches[:MAX_DIAGNOSTIC_SNIPPETS]:
            start = max(0, match.start() - SNIPPET_CONTEXT_CHARS)
            end = min(len(page_text), match.end() + SNIPPET_CONTEXT_CHARS)
            parsed = date_from_match(match, config["months"])
            verdict = "plausible" if is_plausible_delivery_date(parsed) else f"implausible ({parsed})"
            log(f"[{market}]     {match.group().strip()!r} [{verdict}] … {page_text[start:end]} …")
        if len(matches) > MAX_DIAGNOSTIC_SNIPPETS:
            log(f"[{market}]     … and {len(matches) - MAX_DIAGNOSTIC_SNIPPETS} more")
    else:
        log(
            f"[{market}]   [diagnostic] no date-like string anywhere on the page — "
            f"either the page has no date at all, or its month names are missing from "
            f"marketplaces.py['{market}']['months']"
        )

    lowered = page_text.lower()
    shown = 0
    for keyword in DELIVERY_KEYWORDS:
        index = lowered.find(keyword)
        if index == -1:
            continue
        if shown == 0:
            log(f"[{market}]   [diagnostic] delivery-related wording found on the page:")
        start = max(0, index - SNIPPET_CONTEXT_CHARS)
        end = min(len(page_text), index + len(keyword) + SNIPPET_CONTEXT_CHARS * 2)
        log(f"[{market}]     {keyword!r} … {page_text[start:end]} …")
        shown += 1
        if shown >= MAX_DIAGNOSTIC_SNIPPETS:
            break
    if shown == 0:
        # Deliberately not concluding which of these it is — 'page kind'
        # above already answers that, and guessing here would just put a
        # confident wrong sentence in the log.
        log(
            f"[{market}]   [diagnostic] no delivery-related wording anywhere on the page. "
            f"If 'page kind' above shows a product title, this IS a product page and the "
            f"delivery block simply never rendered; if it doesn't, we never reached one."
        )

# Confirmed against real product-page HTML (both an Amazon-sold and a
# third-party-sold listing): #merchantInfoFeature_feature_div always
# holds the actual seller name — "Amazon" when Amazon ships+sells
# ("Shipper / Seller" label), or the third-party name when it doesn't
# ("Sold by" label, e.g. "Skydigital"), regardless of which label wording
# is shown. This replaced an earlier set of guessed selectors
# (#merchant-info, #tabular-buybox, etc.) that don't exist in Amazon's
# actual current markup at all — the fallbacks below are kept only in
# case a market/layout genuinely differs; they remain unverified.
SELLER_CONTAINER_SELECTOR = "#merchantInfoFeature_feature_div"
SELLER_FALLBACK_SELECTORS = [
    "#merchant-info",
    "#tabular-buybox",
    "#aod-offer-soldBy",
    "#usedBuyBoxOOS",
]

MAX_NAME_LENGTH = 40

logger = setup_logger("delivery_multi")


# Ordinal/連 suffixes and connectors that sit between the day number and
# the month name across the pilot's locales: English "1st", French
# "1er", Spanish "15 de enero", Italian "1°", Portuguese-style "1ª".
# Without the Spanish connector, "15 de enero" cannot match at all —
# which is part of why .es returned nothing.
_ORDINAL = r"(?:st|nd|rd|th|er|ère|ème|°|º|ª)?"
_CONNECTOR = r"(?:de\s+|d'|di\s+)?"


def build_date_pattern(months: dict) -> re.Pattern:
    """Match a date in either order, in any of the configured languages.

    Day-first ("21 January 2027", "21. Januar", "15 de enero",
    "12 sierpnia") covers every EU locale here; month-first
    ("January 21, 2027") is how Amazon renders US-English dates and
    shows up on the "/-/en/" override on some markets. The old pattern
    handled day-first only.

    Names are alternated longest-first so an abbreviation can never
    shadow the full name it prefixes ("mar" vs "marca"/"marzo").
    """
    names = "|".join(re.escape(n) for n in sorted(months, key=len, reverse=True))
    day_first = (
        rf"(?<!\d)(?P<d1>\d{{1,2}})\.?{_ORDINAL}\s+{_CONNECTOR}"
        rf"(?P<m1>{names})\.?,?\s*(?:de\s+)?(?P<y1>\d{{4}})?(?!\d)"
    )
    month_first = (
        rf"(?P<m2>{names})\.?\s+(?<!\d)(?P<d2>\d{{1,2}})\.?{_ORDINAL},?"
        rf"\s*(?P<y2>\d{{4}})?(?!\d)"
    )
    return re.compile(rf"(?:{day_first})|(?:{month_first})", re.IGNORECASE)


# build_date_pattern() is called for every parse, including once per
# stored state entry — cache per market rather than recompiling.
_PATTERN_CACHE: dict[frozenset, re.Pattern] = {}


def pattern_for(months: dict) -> re.Pattern:
    key = frozenset(months.items())
    if key not in _PATTERN_CACHE:
        _PATTERN_CACHE[key] = build_date_pattern(months)
    return _PATTERN_CACHE[key]


def date_from_match(match: re.Match, months: dict):
    """Turn a build_date_pattern() match into a real date, or None."""
    day = match.group("d1") or match.group("d2")
    month_name = match.group("m1") or match.group("m2")
    year = match.group("y1") or match.group("y2")
    if not day or not month_name:
        return None
    month = months.get(month_name.lower())
    if not month:
        return None
    today = datetime.date.today()
    try:
        parsed = datetime.date(int(year) if year else today.year, month, int(day))
    except ValueError:
        return None
    # No year printed (Amazon usually omits it) and the date already
    # passed this year -> it must mean next year.
    if not year and parsed < today:
        parsed = parsed.replace(year=parsed.year + 1)
    return parsed


def parse_date_for_sorting(date_str: str, months: dict):
    match = pattern_for(months).search(date_str)
    return date_from_match(match, months) if match else None


def is_plausible_delivery_date(parsed) -> bool:
    if parsed is None:
        return False
    return 0 <= (parsed - datetime.date.today()).days <= MAX_DELIVERY_HORIZON_DAYS


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


def dismiss_cookie_banner(page, market: str) -> bool:
    """Amazon's EU cookie-consent banner ('Accept'/'Alle akzeptieren').
    Production never had to deal with it because the VPS's browser
    profile is warm, but each pilot run starts from a blank context on a
    market it has never visited. On some markets the banner is a
    full-page interstitial rather than an overlay, in which case nothing
    downstream can possibly find a delivery block until it's cleared."""
    try:
        button = page.locator("#sp-cc-accept")
        if button.count() == 0:
            return False
        try:
            # Short timeout: the default 30s is pure waste here. Seen
            # live on amazon.com.be, where a "#redir-modal" backdrop sat
            # over the banner and Playwright retried the click for the
            # full 30 seconds before giving up.
            button.first.click(timeout=5000)
        except PlaywrightTimeoutError:
            # The button is visible and enabled — something is just
            # overlaying it. A DOM-level click ignores the overlay
            # entirely, where a synthetic one can't.
            logger.info(f"[{market}]     cookie banner click intercepted, clicking via DOM instead")
            button.first.evaluate("el => el.click()")
        page.wait_for_timeout(1000)
        logger.info(f"[{market}]     cookie banner accepted")
        return True
    except Exception as e:
        logger.warning(f"[{market}]     cookie banner dismissal failed: {e}")
        return False


def fetch_seller_info(soup) -> str:
    container = soup.select_one(SELLER_CONTAINER_SELECTOR)
    if container:
        # The container repeats the seller name multiple times (visible
        # link, hidden popover-trigger link, popover body) — the first
        # ".offer-display-feature-text-message" match is the clean name
        # ("Amazon" / "Skydigital") without that repetition.
        name_el = container.select_one(".offer-display-feature-text-message")
        text = name_el.get_text(strip=True) if name_el else container.get_text(" ", strip=True)
        if text:
            return text

    for selector in SELLER_FALLBACK_SELECTORS:
        el = soup.select_one(selector)
        if el and el.get_text(strip=True):
            return el.get_text(" ", strip=True)
    return ""


def is_amazon_seller(seller_text: str):
    """True/False/None (unknown, no seller block found at all). Since
    seller_text comes from the seller-specific container (or its clean
    name span), a plain "amazon" substring check is safe here — unlike
    checking the whole page or a shipping/fulfillment block, this text
    never mentions Amazon for reasons other than being the actual
    seller."""
    if not seller_text:
        return None
    return "amazon" in seller_text.lower()


# "International Shopping Transition Alert We are showing you items that
# dispatch to Sweden . To see items that dispatch to a different country,
# change your delivery address." — anchored on "showing you" so it can't
# match the second clause's "dispatch to a different country".
INTERNATIONAL_DESTINATION_PATTERN = re.compile(
    r"showing you items that dispatch to\s+([^.]+?)\s*\.", re.IGNORECASE
)


def international_shopping_destination(page_text_lower: str) -> str:
    """Which country an international-shopping page is dispatching to."""
    match = INTERNATIONAL_DESTINATION_PATTERN.search(page_text_lower)
    if not match:
        return ""
    # Title-cased because the haystack is lowercased for matching, and
    # this string ends up in logs and in stored state.
    return " ".join(match.group(1).split()).title()


def fill_postcode(page, market: str, postcode: str) -> bool:
    """Type the postcode into the glow modal, whichever shape it takes.

    Markets disagree on this, confirmed against real markup:

      .de  one <input id="GLUXZipUpdateInput" maxlength="5">
      .se  TWO inputs, #GLUXZipUpdateInput_0 (maxlength 3) and
           #GLUXZipUpdateInput_1 (maxlength 2), matching how Swedish
           postcodes are written ("371 16"). There is no singular
           #GLUXZipUpdateInput on .se at all — waiting for one is what
           made every .se run fail with an 8s timeout.

    Split fields are filled by each input's own maxlength rather than a
    hardcoded 3/2, so a market that splits differently still works.
    """
    digits = re.sub(r"\D", "", postcode)

    single = page.locator("#GLUXZipUpdateInput")
    if single.count():
        single.first.fill(digits)
        logger.info(f"[{market}]   filled postcode field with {digits}")
        return True

    fields = page.locator("[id^='GLUXZipUpdateInput_']")
    count = fields.count()
    if count == 0:
        logger.warning(f"[{market}]   no postcode field in this modal")
        return False

    # DOM order should already be _0, _1, ... but sort on the id suffix
    # rather than trust it — filling them out of order silently produces
    # a wrong postcode instead of an error.
    indexed = []
    for i in range(count):
        element = fields.nth(i)
        element_id = element.get_attribute("id") or ""
        suffix = element_id.rsplit("_", 1)[-1]
        indexed.append((int(suffix) if suffix.isdigit() else i, element))
    indexed.sort()

    offset = 0
    for _, element in indexed:
        maxlength = element.get_attribute("maxlength")
        take = int(maxlength) if maxlength and maxlength.isdigit() else len(digits) - offset
        element.fill(digits[offset:offset + take])
        offset += take
    logger.info(f"[{market}]   filled {count} split postcode field(s) with {digits}")
    return True


def read_delivery_location(page) -> str:
    """Whatever Amazon's 'Deliver to ...' widget currently says."""
    try:
        ingress = page.locator(GLOW_INGRESS_SELECTOR)
        if ingress.count() == 0:
            return ""
        return " ".join(ingress.first.inner_text().split())
    except Exception:
        return ""


def select_country(page, market: str) -> bool:
    """Pick DELIVERY_COUNTRY in the glow modal's country picker.

    Confirmed against real amazon.de markup (dump_glow_dom.py): the
    picker is a **native `<select id="GLUXCountryList">` with 242
    options**, styled by Amazon's a-dropdown. The visible thing you'd
    click, `#GLUXCountryListDropdown`, is a `<span>`, and the pretty
    option list it opens is a *separate* `<ul role="listbox">` of
    `li.a-dropdown-item > a` that doesn't exist until it's opened — and
    is not a descendant of #GLUXCountryList. An earlier version looked
    for "#GLUXCountryList li a", which therefore matched nothing on
    every market, every time.

    Driving the native <select> is both simpler and sturdier than
    clicking through the popover: Amazon binds its declarative handler
    to the select's change event, so setting it is what actually applies
    the choice. The popover route is kept as a fallback in case some
    market renders the picker differently.
    """
    select = page.locator("#GLUXCountryList")
    if select.count():
        tag = select.first.evaluate("el => el.tagName.toLowerCase()")
        if tag == "select":
            labels = select.first.evaluate(
                "el => Array.from(el.options).map(o => o.text.trim())"
            )
            logger.info(f"[{market}]   country <select> offers {len(labels)} option(s)")
            if DELIVERY_COUNTRY not in labels:
                logger.warning(
                    f"[{market}]   {DELIVERY_COUNTRY!r} is not in this market's country list — "
                    f"it may not ship there. First 12: {labels[:12]}"
                )
                return False
            # Exact label match, never substring: "United States" must
            # not select "United States Minor Outlying Islands".
            select.first.select_option(label=DELIVERY_COUNTRY, timeout=5000)
            logger.info(f"[{market}]   selected {DELIVERY_COUNTRY!r} in the native <select>")
            return True
        logger.info(f"[{market}]   #GLUXCountryList is a <{tag}>, not a <select> — trying the popover")

    # Fallback: open the styled dropdown and click the rendered list.
    dropdown = page.locator("#GLUXCountryListDropdown")
    if dropdown.count() == 0:
        # Expected on the domestic market: amazon.se's modal offers only
        # "sign in" or a Swedish postcode — there's no ship-to-another-
        # country option on your own country's site. Info, not a
        # warning; the caller warns loudly if the postcode fallback also
        # fails to apply anything.
        logger.info(f"[{market}]   no country picker in this modal (expected on the domestic market)")
        return False
    dropdown.first.click(timeout=5000)
    page.wait_for_timeout(1000)

    options = page.locator("[role='listbox'] li.a-dropdown-item a, .a-dropdown-item a")
    labels = [" ".join(options.nth(i).inner_text().split()) for i in range(options.count())]
    logger.info(f"[{market}]   popover list offers {len(labels)} option(s)")
    if DELIVERY_COUNTRY not in labels:
        logger.warning(
            f"[{market}]   {DELIVERY_COUNTRY!r} not in the popover list. First 12: {labels[:12]}"
        )
        return False
    options.nth(labels.index(DELIVERY_COUNTRY)).click(timeout=5000)
    logger.info(f"[{market}]   selected {DELIVERY_COUNTRY!r} via the popover list")
    return True


def set_delivery_location(page, market: str, config: dict) -> str:
    """Pin the delivery destination to DELIVERY_COUNTRY/DELIVERY_POSTCODE.

    Two different modal shapes, depending on whether the destination is
    the marketplace's own country:

      * domestic  -> a postcode field (#GLUXZipUpdateInput) + Apply.
                     Only valid domestically: the field is validated
                     against the marketplace's own country ("Please
                     enter a five-digit German postcode" on .de) and
                     carries that country's maxlength.
      * elsewhere -> the country picker, no postcode, because Amazon
                     only quotes country-level estimates across borders

    Called once per market, after warm-up: the choice is stored in the
    session cookies, so every product page in that context inherits it.

    Never raises. A failure here doesn't invalidate the run, it just
    means the dates describe Amazon's guessed destination instead of the
    requested one — so it returns whatever the widget ends up saying and
    lets the caller log the discrepancy loudly.

    Selectors are taken from a real amazon.de modal captured with
    dump_glow_dom.py, not guessed. Every step still logs what it found,
    so a market whose modal differs is diagnosable from the log alone.
    """
    domestic = config["country"].strip().lower() == DELIVERY_COUNTRY.strip().lower()
    logger.info(
        f"[{market}] pinning delivery location to {DELIVERY_COUNTRY!r}"
        + (f" (postcode {DELIVERY_POSTCODE} as fallback)" if domestic else "")
        + f"; currently reads {read_delivery_location(page)!r}"
    )

    try:
        for selector in GLOW_OPENER_SELECTORS:
            opener = page.locator(selector)
            if opener.count():
                opener.first.click(timeout=5000)
                logger.info(f"[{market}]   opened location picker via {selector!r}")
                break
        else:
            logger.warning(f"[{market}]   no location picker on this page — leaving location as-is")
            return read_delivery_location(page)

        page.wait_for_timeout(1500)

        # Country first, on EVERY market including the domestic one.
        # Expressing the destination as a country is the only form every
        # modal supports, and it keeps all markets answering the same
        # question. It's also what .se needs: its modal never rendered
        # #GLUXZipUpdateInput at all (8s timeout on a real run), so the
        # domestic-postcode path simply didn't work there.
        if not select_country(page, market):
            if not domestic:
                return read_delivery_location(page)
            # Domestic fallback: a modal with no usable country picker
            # can still take a postcode, and postcode-level estimates
            # are more precise than country-level ones.
            logger.info(f"[{market}]   no country selection; using postcode {DELIVERY_POSTCODE}")
            if not fill_postcode(page, market, DELIVERY_POSTCODE):
                return read_delivery_location(page)
            # #GLUXZipUpdate is a <span> wrapping the real
            # <input type="submit">; click the input directly.
            apply_button = page.locator("#GLUXZipUpdate input.a-button-input")
            (apply_button if apply_button.count() else page.locator("#GLUXZipUpdate")).first.click(timeout=5000)
            logger.info(f"[{market}]   submitted postcode {DELIVERY_POSTCODE}")

        # Applying either way swaps in a success panel whose "Continue"
        # button (#GLUXConfirmClose) starts hidden inside
        # #GLUXHiddenSuccessDialog — so it becoming visible is itself
        # confirmation that Amazon accepted the change, not just that we
        # clicked something. Fall back to the modal's plain "Done".
        page.wait_for_timeout(1500)
        for selector in ("#GLUXConfirmClose", "[name='glowDoneButton']"):
            button = page.locator(selector)
            if button.count() and button.first.is_visible():
                button.first.click(timeout=5000)
                logger.info(f"[{market}]   confirmed via {selector!r}")
                break
        else:
            logger.info(f"[{market}]   no confirm/done button became visible")

        # The change is applied server-side against the session; reload
        # so the widget (and everything downstream) reflects it.
        page.wait_for_timeout(2500)
        page.reload(wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)
    except Exception as e:
        logger.warning(f"[{market}]   could not set delivery location: {e}")

    return read_delivery_location(page)


def safe_goto(page, url: str, market: str) -> None:
    """Navigate, tolerate Amazon's spurious download prompt, then settle
    the page (cookie banner + any chained interstitial).

    Every navigation must go through this. The retry path below used to
    call page.goto() directly, and a real run showed exactly why that
    was wrong: two .se products hit "Page.goto: Download is starting" on
    the retry, which propagated out and killed the item as an ERROR even
    though the initial navigation had handled the identical condition
    fine three times in the same run.
    """
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        # Amazon occasionally triggers a spurious download prompt on
        # navigation (seen live: "Download is starting") — the catalog
        # scraper already tolerates this; the delivery tracker didn't,
        # so one flaky nav crashed the item AND cascaded into
        # "navigation interrupted" errors for every item after it, since
        # the aborted goto left the page mid-transition.
        if "Download is starting" in str(e):
            logger.info(f"[{market}]   navigation triggered a spurious download prompt, continuing anyway")
            page.wait_for_timeout(1000)
        else:
            raise

    page.wait_for_timeout(2000)
    dismiss_cookie_banner(page, market)
    for attempt in range(2):
        if not dismiss_continue_shopping_interstitial(page, market):
            break
        logger.info(f"[{market}]   re-checking for a chained interstitial (attempt {attempt + 1}/2)")


def fetch_delivery_date(page, url: str, asin: str, market: str, config: dict, date_pattern: re.Pattern) -> dict:
    logger.info(f"[{market}]   navigating to {url}")
    safe_goto(page, url, market)
    logger.info(f"[{market}]   page loaded (domcontentloaded), settling...")

    # A debug dump captured for .se ASIN B0G4NF8QDZ turned out to be the
    # plain Amazon.se homepage (title "Amazon.se: Low Prices...",
    # canonical "/-/en/", zero occurrences of the ASIN anywhere in the
    # HTML) — proof a goto can silently land somewhere other than the
    # product page and get read downstream as "delivery block just isn't
    # there" instead of "we're not even on the right page". A real run
    # also produced 'chrome-error://chromewebdata/' here, the aftermath
    # of an aborted download-prompt navigation. Catch both explicitly
    # rather than inferring them from missing selectors after the fact.
    for attempt in range(1, 3):
        if asin in page.url:
            break
        logger.warning(
            f"[{market}]   landed on {page.url!r} instead of the product page for {asin} "
            f"— retrying navigation ({attempt}/2)"
        )
        safe_goto(page, url, market)
    else:
        logger.warning(f"[{market}]   still on {page.url!r} after 2 retries — giving up on {asin} for this pass")
        return {"date": "UNKNOWN — navigation didn't reach the product page", "seller_text": "", "is_amazon_seller": None}

    # Delivery/seller blocks are often injected client-side via JS after
    # domcontentloaded — confirmed via a real .de page's raw server HTML,
    # which had neither present at all despite being a normal, purchasable
    # listing (Add-to-Cart, buybox all present). A fixed 2s sleep isn't
    # always enough for that JS to finish, even on .se (which still saw
    # some UNKNOWN results in a full test run). Wait adaptively for any
    # target selector to appear, falling through to the existing
    # not-found handling on timeout rather than failing outright.
    combined_selector = ", ".join(
        CANDIDATE_SELECTORS + AVAILABILITY_SELECTORS + [SELLER_CONTAINER_SELECTOR]
    )
    try:
        page.wait_for_selector(combined_selector, timeout=6000)
        logger.info(f"[{market}]   a target selector appeared")
    except PlaywrightTimeoutError:
        logger.info(f"[{market}]   no target selector appeared within 6s — proceeding anyway")

    logger.info(f"[{market}]   reading page content")
    html = page.content()
    soup = BeautifulSoup(html, "html.parser")

    # In --debug mode, dump what we're looking at for every product. In
    # normal mode, stay quiet unless the result is UNKNOWN — which is
    # exactly the case you'd need the dump to explain — and emit it then
    # (see the UNKNOWN return path below).
    def emit_diagnostics(log) -> None:
        log_page_snapshot(market, asin, page, soup, html, log)
        log_text_probes(market, soup, date_pattern, config, log)

    if DEBUG:
        emit_diagnostics(logger.info)

    # Amazon can serve an "international shopping" variant of a product
    # page when it geolocates the visitor outside the marketplace's
    # country. Observed live on amazon.de from a US-based GitHub Actions
    # runner: all 8 products came back with an "International Shopping
    # Transition Alert ... we are showing you items that dispatch to
    # United States" banner, no add-to-cart, no price block, an empty
    # #availability — and, on the three that did render a delivery
    # block, prices and dates quoted in USD for shipping to the US.
    #
    # Those USD dates are real dates, but they are the wrong ones: they
    # describe international shipping to the runner's country, not what
    # a local customer sees. Storing one would be precisely the
    # confident-but-wrong result this scraper is supposed to avoid — and
    # it would become the baseline a future "moved earlier" alert fires
    # against. Report it as UNKNOWN (state preserved, no alert) and say
    # why, loudly.
    #
    # This is an artefact of WHERE the scraper runs, not a bug: on the
    # European VPS this pilot is destined for, it shouldn't trigger at
    # all. Detected rather than worked around, because faking a delivery
    # location is fragile and would hide the fact that a run's numbers
    # aren't comparable to a local one's.
    page_text_lower = soup.get_text(" ", strip=True).lower()
    if "international shopping transition alert" in page_text_lower:
        shipping_to = international_shopping_destination(page_text_lower)
        if shipping_to and shipping_to.lower() == DELIVERY_COUNTRY.lower():
            # Intended. We asked amazon.de to deliver to Sweden, so of
            # course it's showing us the cross-border experience — that
            # IS the question ("can I get this from .de, to me, and
            # when?"). Read the page normally.
            logger.info(
                f"[{market}]   international-shopping page dispatching to {shipping_to} "
                f"— that's the requested destination, reading it normally"
            )
        else:
            logger.warning(
                f"[{market}] {asin}: page is dispatching to {shipping_to or 'an unknown country'}, "
                f"but we asked for {DELIVERY_COUNTRY} — so any date here describes the wrong "
                f"destination. Not trusting it."
            )
            if not DEBUG:
                emit_diagnostics(logger.warning)
            return {
                "date": f"UNKNOWN — page dispatches to {shipping_to or 'an unknown country'}, not {DELIVERY_COUNTRY}",
                "seller_text": "",
                "is_amazon_seller": None,
            }

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
            parsed = date_from_match(match, config["months"])
            # Sanity-check before trusting it. A delivery block can
            # legitimately contain text we mis-anchor on, and a bogus
            # date here is worse than no date: it gets stored as the
            # baseline and can fire a "moved earlier" alert.
            if is_plausible_delivery_date(parsed):
                logger.info(f"[{market}]   date pattern matched: {date_str!r} -> {parsed}")
                return result(date_str)
            logger.warning(
                f"[{market}]   rejecting implausible date {date_str!r} (parsed as {parsed}) "
                f"from {matched_selector!r} — not within 0..{MAX_DELIVERY_HORIZON_DAYS} days"
            )
        else:
            logger.info(f"[{market}]   delivery block matched but no date pattern in {text_blob[:200]!r}")
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

    # Signals are matched against the availability/buybox region only —
    # NOT the whole page (see AVAILABILITY_SELECTORS). If none of those
    # containers exist we genuinely know nothing about this page, so we
    # fall through to UNKNOWN + a debug dump instead of inventing a
    # state from whatever prose happens to be lying around.
    availability_parts = []
    for selector in AVAILABILITY_SELECTORS:
        for el in soup.select(selector):
            text = el.get_text(" ", strip=True)
            if text:
                availability_parts.append(text)

    if availability_parts:
        signal_text = " ".join(availability_parts).lower()
        logger.info(
            f"[{market}]   checking signal phrases against availability region "
            f"({len(signal_text)} chars): {_clip(signal_text, 240)!r}"
        )
        # Checked before the generic signals because it's the most
        # specific and the most actionable: the listing exists and may
        # well be in stock, Amazon just won't send it to where we asked.
        # Seen live on .de with the destination pinned to Sweden. Worth
        # its own outcome rather than being flattened into "no date" —
        # "they won't ship it to me" and "they haven't said when" are
        # different answers to the question being asked.
        if "cannot be dispatched to your selected delivery location" in signal_text:
            logger.info(f"[{market}]   item cannot be dispatched to {DELIVERY_COUNTRY}")
            return result(f"NOT DELIVERABLE (this market won't dispatch it to {DELIVERY_COUNTRY})")

        for signal in config["out_of_stock_signals"]:
            if signal in signal_text:
                logger.info(f"[{market}]   matched out-of-stock signal: {signal!r}")
                return result("OUT OF STOCK (temporarily unavailable, no delivery date yet)")

        for signal in config["no_date_signals"]:
            if signal in signal_text:
                logger.info(f"[{market}]   matched no-date signal: {signal!r}")
                return result("NO DATE YET (listing has no confirmed delivery estimate)")
        # A listing with no featured offer ("buybox winner") shows only a
        # "See All Buying Options" button — no Add to Cart, no price
        # block, an empty #availability, and consequently no delivery
        # block at all, because there's no offer to quote a date for.
        # Every UNKNOWN in the 2026-08-04 VPS run was one of these
        # (mostly cross-border listings), and they were indistinguishable
        # from a genuine scrape failure. They are a real, nameable state,
        # so name it — that keeps UNKNOWN meaning "we don't understand
        # this page", which is the only way it stays a useful signal.
        #
        # Gated on Add-to-Cart being absent as well as the phrase being
        # present: pages that DO have a featured offer can also link to
        # all buying options, and misreading one of those would hide a
        # real failure.
        if "see all buying options" in signal_text and not soup.select_one("#add-to-cart-button"):
            logger.info(f"[{market}]   no featured offer (only 'See All Buying Options', no Add to Cart)")
            return result("NO OFFER (no featured offer on this listing, so no delivery estimate)")
        logger.info(f"[{market}]   no signal phrase matched in the availability region")
    else:
        logger.info(f"[{market}]   no availability/buybox container found — skipping signal check")

    debug_dir = PILOT_DIR / "state" / market
    debug_dir.mkdir(parents=True, exist_ok=True)
    # Name from the ASIN we were asked for, not from the URL's last
    # segment — the URL may have been redirected somewhere else entirely,
    # and "which ASIN was this dump for" is the only question the
    # filename needs to answer.
    debug_file = debug_dir / f"debug_{asin}.html"
    debug_file.write_text(html, encoding="utf-8")
    logger.warning(f"[{market}]   no match at all for {asin} — saved {debug_file}")
    # The artifact holding that HTML is frequently un-downloadable (see
    # the Diagnostics section comment), so summarise it into the log too.
    if not DEBUG:
        emit_diagnostics(logger.warning)
    return result("UNKNOWN — no date found, saved debug HTML")


def get_product_title(page) -> str:
    """Amazon titles read "Product Name : Amazon.de: Toys". Split on the
    " : Amazon" marker specifically rather than a bare " :" — product
    names themselves contain colons often enough (e.g. "Beyblade X:
    Xtreme Battle") that the loose split was truncating real names."""
    title = page.title()
    if not title:
        return "Unknown product"
    for marker in (" : Amazon", " | Amazon", ": Amazon"):
        if marker in title:
            return title.split(marker)[0].strip()
    return title.strip()


def truncate_name(name: str) -> str:
    return name if len(name) <= MAX_NAME_LENGTH else name[: MAX_NAME_LENGTH - 1].rstrip() + "…"


def check_market(market: str, asins: list[str], send_discord: bool) -> dict:
    config = MARKETPLACES[market]
    market_dir = PILOT_DIR / "state" / market
    market_dir.mkdir(parents=True, exist_ok=True)
    state_file = market_dir / "delivery_state.json"

    if not asins:
        return {}

    logger.info(f"=== [{market}] Checking {len(asins)} product(s) ===")

    # Per-market outcome tally. The single number worth watching across
    # runs is "real date" — that's the hit rate the pilot is being
    # judged on, and it's tedious to count by hand from the log.
    outcomes = {"real date": 0, "NO DATE YET": 0, "OUT OF STOCK": 0, "NO OFFER": 0,
                "NOT DELIVERABLE": 0, "UNKNOWN": 0, "ERROR": 0}
    # ASINs this pass actually got a fresh answer for, so the summary
    # can distinguish them from carried-over state (see below).
    refreshed: set[str] = set()

    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    date_pattern = build_date_pattern(config["months"])

    # A real delivery estimate can never legitimately be in the past — so
    # any stored date that's already passed is evidence of past
    # corruption (e.g. the full-page-text date-regex false positives this
    # replaced), not a value worth protecting via "keep previous state on
    # UNKNOWN." Discard it so a genuinely unavailable date reads as
    # missing rather than as a stale, misleading one.
    # Widened from "in the past" to the same plausibility window used
    # when accepting a fresh date, so a bogus far-future date written by
    # an earlier buggy run is cleaned out too rather than sitting there
    # forever as an unbeatable "earliest known" baseline.
    for asin in list(state.keys()):
        raw = state[asin].get("date", "")
        parsed = parse_date_for_sorting(raw, config["months"])
        if parsed is not None and not is_plausible_delivery_date(parsed):
            logger.warning(f"[{market}] discarding stale state for {asin}: {raw!r} is outside the plausible window")
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

        # Warm up on the "/-/en/" homepage, not the bare domain. The bare
        # domain sets the session's language cookie to the market's
        # native language, which then has to be fought off by the
        # "/-/en/" prefix on every subsequent product URL; warming up on
        # the English homepage sets that cookie to English up front so
        # the whole session agrees on one language. Also gets the cookie
        # banner out of the way once instead of per product.
        warmup_url = f"https://www.{config['domain']}/-/en/"
        delivery_location = ""
        logger.info(f"[{market}] warming up: navigating to {warmup_url}")
        try:
            page.goto(warmup_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)
            dismiss_cookie_banner(page, market)
            dismiss_continue_shopping_interstitial(page, market)
            logger.info(f"[{market}] warm-up complete")
            delivery_location = set_delivery_location(page, market, config)
            # Loud, because it changes what every date in this run means.
            # Substring check both ways: the widget renders the country
            # alone for international ("Sweden") but city + postcode for
            # domestic ("Stockholm 111 64"), so neither is a prefix of a
            # fixed expected string.
            if delivery_location and (
                DELIVERY_COUNTRY.lower() in delivery_location.lower()
                or DELIVERY_POSTCODE.replace(" ", "") in delivery_location.replace(" ", "")
            ):
                logger.info(f"[{market}] delivery location confirmed: {delivery_location!r}")
            else:
                logger.warning(
                    f"[{market}] DELIVERY LOCATION NOT APPLIED — widget reads {delivery_location!r}, "
                    f"wanted {DELIVERY_COUNTRY}/{DELIVERY_POSTCODE}. Dates from this market describe "
                    f"delivery to Amazon's guessed destination and are NOT comparable to the others."
                )
        except Exception as e:
            logger.warning(f"[{market}] homepage warm-up failed: {e}")

        for i, asin in enumerate(asins, start=1):
            # "/-/en/" is Amazon's language-override path segment, forcing
            # English page text on non-English domains. Confirmed reaching
            # the real product page (not a redirect) via a real .de
            # product's raw server HTML (view-source, actual product
            # title present). It was reverted for one run on the theory
            # that it served incomplete content, based on de/es/it/pl
            # staying at 0/8 real dates — but a debug dump from that same
            # run turned out to be the .se homepage, not a product page,
            # which points at the known "Download is starting" nav
            # cascade (now caught, see fetch_delivery_date) as the actual
            # cause, not the URL form. Restored; fetch_delivery_date now
            # also verifies the ASIN is actually in page.url() before
            # trusting the page content, so a stray redirect/cascade is
            # caught and retried instead of silently misread.
            url = f"https://www.{config['domain']}/-/en/dp/{asin}"
            logger.info(f"[{market}] ({i}/{len(asins)}) checking {asin}")
            start = time.monotonic()
            try:
                result = fetch_delivery_date(page, url, asin, market, config, date_pattern)
                name = get_product_title(page)
            except Exception as e:
                logger.warning(f"[{market}] ({i}/{len(asins)}) {asin} failed after {time.monotonic() - start:.1f}s: {e}")
                outcomes["ERROR"] += 1
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
                outcomes["UNKNOWN"] += 1
                logger.warning(f"[{market}] {asin}: {current_date} — keeping previous state")
                continue

            refreshed.add(asin)
            if current_date.startswith("OUT OF STOCK"):
                outcomes["OUT OF STOCK"] += 1
            elif current_date.startswith("NO DATE YET"):
                outcomes["NO DATE YET"] += 1
            elif current_date.startswith("NO OFFER"):
                outcomes["NO OFFER"] += 1
            elif current_date.startswith("NOT DELIVERABLE"):
                outcomes["NOT DELIVERABLE"] += 1
            else:
                outcomes["real date"] += 1

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

    # Rows not refreshed this pass are flagged rather than silently
    # shown as current. The 2026-08-04 VPS run made the problem obvious:
    # .de and .fr both listed a Tide Whale date ("5. August" / "5 août")
    # that no product returned in that run — leftovers from an older run,
    # still in their pre-language-fix native format, printed next to
    # fresh results with nothing to tell them apart. Keeping the stored
    # value on UNKNOWN is deliberate (it's the previous baseline), but
    # presenting it as this run's answer is not.
    rows = [
        (
            parse_date_for_sorting(info["date"], config["months"]),
            truncate_name(info["name"]),
            info["date"],
            info.get("is_amazon_seller"),
            asin in refreshed,
        )
        for asin, info in state.items()
        if asin in asins
    ]
    rows.sort(key=lambda r: (r[0] is None, r[0]))

    name_width = max((len(name) for _, name, _, _, _ in rows), default=len("Product"))
    logger.info(f"DELIVERY SUMMARY [{market.upper()}] — {time.strftime('%Y-%m-%d %H:%M:%S')}")
    for _, name, date_str, is_amazon, is_fresh in rows:
        flag = " ⚠️ NOT AMAZON" if is_amazon is False else ("" if is_amazon else " (seller unknown)")
        stale = "" if is_fresh else "  ⏳ STALE — not refreshed this run"
        logger.info(f"  {name:<{name_width}} | {date_str}{flag}{stale}")

    total = sum(outcomes.values())
    tally = "  ".join(f"{label}={count}" for label, count in outcomes.items())
    logger.info(
        f"OUTCOMES [{market.upper()}] {tally}  (real dates: {outcomes['real date']}/{total})"
        f"  [delivering to: {delivery_location or 'UNKNOWN'}]"
    )
    if outcomes["UNKNOWN"] and not DEBUG:
        logger.info(
            f"[{market}] {outcomes['UNKNOWN']} UNKNOWN result(s) — each one printed a page "
            f"snapshot above. Rerun with --debug (or the workflow's debug input) to get the "
            f"same snapshot for the products that DID resolve, for comparison."
        )

    return outcomes


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "markets",
        nargs="*",
        default=DEFAULT_MARKETS,
        help=f"Market codes to check (default: {' '.join(DEFAULT_MARKETS)}; "
             f"all configured: {' '.join(MARKETPLACES)})",
    )
    parser.add_argument("--send-discord", action="store_true", help="Actually post to Discord instead of just logging what would be sent")
    parser.add_argument(
        "--debug",
        action="store_true",
        default=DEBUG,
        help="Log a full page snapshot (selector-by-selector presence, page language, "
             "date-like strings, delivery wording) for EVERY product, not just the ones "
             "that come back UNKNOWN. Also settable via DEBUG=true.",
    )
    args = parser.parse_args()
    DEBUG = args.debug

    logger.info(f"=== START delivery_multi markets={args.markets} debug={DEBUG} ===")
    log_run_config(args.markets)
    try:
        tracked_asins = load_whitelist()
        if not tracked_asins:
            logger.info("Nothing to track — exiting.")
        else:
            per_market = {}
            for market in args.markets:
                if market not in MARKETPLACES:
                    logger.error(f"Unknown market '{market}' — choices are {list(MARKETPLACES)}")
                    continue
                per_market[market] = check_market(market, tracked_asins, args.send_discord)

            # Cross-market scoreboard. This is the line to compare
            # between runs when judging whether a change helped — the
            # whole reason the pilot exists is that the hit rate varies
            # wildly by market.
            logger.info("=== HIT RATE BY MARKET ===")
            for market, outcomes in per_market.items():
                total = sum(outcomes.values())
                if not total:
                    continue
                hits = outcomes["real date"]
                logger.info(
                    f"  {market:<3} real dates {hits}/{total}"
                    f"   ({'  '.join(f'{k}={v}' for k, v in outcomes.items() if v)})"
                )
        logger.info("=== END delivery_multi (exit 0) ===")
    except Exception:
        logger.exception("=== END delivery_multi (exit 1) ===")
        sys.exit(1)
