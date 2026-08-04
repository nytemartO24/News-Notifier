"""Shared Amazon browser plumbing for the EU multi-market pilot.

Everything in here is about *getting a usable page in front of you* —
launching Chromium, clearing Amazon's cookie banner and bot
interstitials, and pinning the delivery destination — plus the Discord
notifier. None of it knows anything about delivery dates or catalogues,
so both pilot scrapers can share it.

It was extracted from track_delivery_multi.py once a second scraper
needed the same location-pinning: that code took several rounds of live
debugging against real Amazon markup to get right (see set_delivery_location
and fill_postcode), and having two copies of it drift apart would be a
guaranteed source of "why does one scraper see different dates".

Logging goes to whichever script imported this — call use_logger() with
your own logger at startup so the lines land in that script's log file.
"""

import logging
import os
import re
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
DISCORD_USER_ID = os.environ.get("DISCORD_USER_ID", "")
HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"

# Where we're pretending to be, for delivery-estimate purposes.
#
# This is not cosmetic. A delivery date only means anything relative to a
# destination, and Amazon picks one by geolocation if you don't tell it —
# which produced a genuinely useless run on 2026-08-04: se/de/nl/be/pl
# each resolved to a local address (Stockholm, Nuremberg, Amsterdam,
# Brussels, Warsaw) while fr/es/it all resolved to "Deliver to Germany"
# (the VPS's own country) and served offer-less cross-border pages. So no
# two markets were answering the same question, and only .se was
# answering the one that matters: "would this arrive at MY address".
#
# The postcode applies to the domestic market only — amazon.se's modal
# has no country picker, while the other markets' postcode fields are
# validated against their own country and so can't express Sweden.
# Written without the space Swedish postcodes normally carry ("371 16");
# fill_postcode() distributes the digits across the modal's fields.
DELIVERY_COUNTRY = os.environ.get("DELIVERY_COUNTRY", "Sweden")
DELIVERY_POSTCODE = os.environ.get("DELIVERY_POSTCODE", "37116")

# Amazon's "Deliver to ..." location widget ("glow"). One opener, not a
# list of candidates: this is the element that opened the modal on all
# four markets, every run.
GLOW_INGRESS_SELECTOR = "#glow-ingress-line2"
GLOW_OPENER_SELECTOR = "#nav-global-location-popover-link"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

logger = logging.getLogger("pilot.browser")


def use_logger(new_logger: logging.Logger) -> None:
    """Send this module's output to the calling script's logger."""
    global logger
    logger = new_logger


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


def fill_postcode(page, market: str, postcode: str) -> bool:
    """Type the postcode into the glow modal, whichever shape it takes.

    amazon.se splits it across TWO inputs — #GLUXZipUpdateInput_0
    (maxlength 3) and _1 (maxlength 2) — matching how Swedish postcodes
    are written ("371 16"). Other markets use a single
    #GLUXZipUpdateInput. The prefix selector matches both shapes, so one
    loop covers them without branching; digits are distributed by each
    field's own maxlength rather than a hardcoded 3/2.
    """
    digits = re.sub(r"\D", "", postcode)

    fields = page.locator("[id^='GLUXZipUpdateInput']")
    count = fields.count()
    if count == 0:
        logger.warning(f"[{market}]   no postcode field in this modal")
        return False

    # DOM order should already be _0, _1, ... but sort on the id suffix
    # rather than trust it — filling them out of order silently produces
    # a different but structurally valid postcode instead of an error.
    indexed = []
    for i in range(count):
        element = fields.nth(i)
        suffix = (element.get_attribute("id") or "").rsplit("_", 1)[-1]
        indexed.append((int(suffix) if suffix.isdigit() else i, element))
    indexed.sort()

    offset = 0
    for _, element in indexed:
        maxlength = element.get_attribute("maxlength")
        take = int(maxlength) if maxlength and maxlength.isdigit() else len(digits) - offset
        element.fill(digits[offset:offset + take])
        offset += take
    logger.info(f"[{market}]   filled {count} postcode field(s) with {digits}")
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

    Driving the native <select> is what actually applies the choice —
    Amazon binds its declarative handler to the select's change event —
    and it's sturdier than clicking a popover that doesn't exist until
    opened. Verified on de/fr/es (242/244/250 options each); the popover
    route was never needed and has been removed.
    """
    select = page.locator("#GLUXCountryList")
    if select.count() == 0:
        logger.warning(f"[{market}]   no country picker (#GLUXCountryList) in this modal")
        return False

    labels = select.first.evaluate("el => Array.from(el.options).map(o => o.text.trim())")
    logger.info(f"[{market}]   country <select> offers {len(labels)} option(s)")
    if DELIVERY_COUNTRY not in labels:
        logger.warning(
            f"[{market}]   {DELIVERY_COUNTRY!r} is not in this market's country list — "
            f"it may not ship there. First 12: {labels[:12]}"
        )
        return False
    # Exact label match, never substring: "United States" must not
    # select "United States Minor Outlying Islands".
    select.first.select_option(label=DELIVERY_COUNTRY, timeout=5000)
    logger.info(f"[{market}]   selected {DELIVERY_COUNTRY!r} in the native <select>")
    return True


def set_delivery_location(page, market: str, config: dict) -> str:
    """Pin the delivery destination to DELIVERY_COUNTRY/DELIVERY_POSTCODE.

    Which mechanism to use is decided by the market, not discovered by
    trying things — both were verified live on 2026-08-04:

      domestic (.se)          postcode. Its modal has no country picker
                              at all, and a postcode is more precise
                              than a country anyway.
      elsewhere (.de/.fr/.es) country picker. The postcode field there
                              is validated against the marketplace's own
                              country ("Please enter a five-digit German
                              postcode"), so it cannot express Sweden.

    There is deliberately no cross-fallback between the two: neither can
    do the other's job, so "try the other one" could only ever turn a
    clear failure into a confusing one.

    Called once per market, after warm-up: the choice is stored in the
    session cookies, so every product page in that context inherits it.

    Never raises. A failure here doesn't invalidate the run, it just
    means the dates describe Amazon's guessed destination instead of the
    requested one — so it returns whatever the widget ends up saying and
    lets the caller log the discrepancy loudly.
    """
    domestic = config["country"].strip().lower() == DELIVERY_COUNTRY.strip().lower()
    logger.info(
        f"[{market}] pinning delivery location to "
        + (f"postcode {DELIVERY_POSTCODE}" if domestic else DELIVERY_COUNTRY)
        + f"; currently reads {read_delivery_location(page)!r}"
    )

    try:
        opener = page.locator(GLOW_OPENER_SELECTOR)
        if opener.count() == 0:
            logger.warning(f"[{market}]   no location picker on this page — leaving location as-is")
            return read_delivery_location(page)
        opener.first.click(timeout=5000)
        page.wait_for_timeout(1500)

        if domestic:
            if not fill_postcode(page, market, DELIVERY_POSTCODE):
                return read_delivery_location(page)
            # #GLUXZipUpdate is a <span> wrapping the real
            # <input type="submit">; click the input directly.
            page.locator("#GLUXZipUpdate input.a-button-input").first.click(timeout=5000)
            logger.info(f"[{market}]   submitted postcode {DELIVERY_POSTCODE}")
        elif not select_country(page, market):
            return read_delivery_location(page)

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


def open_market(playwright, market: str, config: dict):
    """Launch a browser for one marketplace, warmed up and pinned.

    Returns (browser, page). Close the browser yourself when done.

    The warm-up navigates to the "/-/en/" homepage rather than the bare
    domain: the bare domain sets the session's language cookie to the
    market's native language, which then has to be fought off by the
    "/-/en/" prefix on every subsequent URL. Warming up in English sets
    that cookie correctly up front, and clears the cookie banner once
    instead of on every page.

    A failure to pin the location is non-fatal but logged loudly — any
    market showing DELIVERY LOCATION NOT APPLIED is describing a
    destination Amazon guessed, and is not comparable to the others.
    """
    logger.info(f"[{market}] launching Chromium (headless={HEADLESS})")
    browser = playwright.chromium.launch(headless=HEADLESS, channel="chromium")
    page = browser.new_context(user_agent=USER_AGENT, locale=config["locale"]).new_page()

    warmup_url = f"https://www.{config['domain']}/-/en/"
    logger.info(f"[{market}] warming up: navigating to {warmup_url}")
    try:
        page.goto(warmup_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)
        dismiss_cookie_banner(page, market)
        dismiss_continue_shopping_interstitial(page, market)
        logger.info(f"[{market}] warm-up complete")
        location = set_delivery_location(page, market, config)
        # Substring check both ways: the widget renders the country alone
        # for international ("Sweden") but city + postcode for domestic
        # ("Karlskrona 371 16"), so neither is a prefix of a fixed string.
        if location and (
            DELIVERY_COUNTRY.lower() in location.lower()
            or DELIVERY_POSTCODE.replace(" ", "") in location.replace(" ", "")
        ):
            logger.info(f"[{market}] delivery location confirmed: {location!r}")
        else:
            logger.warning(
                f"[{market}] DELIVERY LOCATION NOT APPLIED — widget reads {location!r}, "
                f"wanted {DELIVERY_COUNTRY}/{DELIVERY_POSTCODE}. Results from this market "
                f"describe Amazon's guessed destination and are NOT comparable to the others."
            )
    except Exception as e:
        logger.warning(f"[{market}] warm-up failed: {e}")
        location = ""

    return browser, page, location
