#!/usr/bin/env python3
"""
Send sample alerts to Discord to check the webhook and the formatting.

Deliberately calls the REAL formatters in alerts.py and the real
notify() in browser.py — a test that rendered its own idea of the
message would pass happily while what you actually receive is broken.
The only thing faked is the scraped data.

Usage:
    python pilot/eu_multimarket/test_discord.py               # print only
    python pilot/eu_multimarket/test_discord.py --send-discord  # actually post

Env vars:
    DISCORD_WEBHOOK_URL   required for --send-discord
    DISCORD_USER_ID       optional; adds the @mention
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import alerts
import browser
from browser import notify
from logging_setup import setup_logger
from marketplaces import MARKETPLACES

logger = setup_logger("test_discord")
browser.use_logger(logger)

# A believable run: Curse Mummy improves on two markets at once (the
# case that motivated one-message-per-ASIN), stays unavailable on the
# other two. Dates and states are lifted from real runs.
SAMPLE_ASIN = "B0G4NF8QDZ"
SAMPLE_NAME = "Hasbro Beyblade X Curse Mummy 7-55W UX Booster Pack Set with Takara Tomy Spinning Top"
SAMPLE_ROWS = [
    {"market": "se", "previous": "11 February, 2027", "current": "9 September",
     "improved": True, "price": "249,00 kr", "day": 9},
    {"market": "de", "previous": "NOT DELIVERABLE (this market won't dispatch it to Sweden)",
     "current": "NOT DELIVERABLE (this market won't dispatch it to Sweden)",
     "improved": False, "price": "", "day": None},
    {"market": "fr", "previous": "OUT OF STOCK (temporarily unavailable, no delivery date yet)",
     "current": "12 August", "improved": True, "price": "24,99 €", "day": 12},
    {"market": "es", "previous": "NO DATE YET (listing has no confirmed delivery estimate)",
     "current": "NO DATE YET (listing has no confirmed delivery estimate)",
     "improved": False, "price": "", "day": None},
]

SAMPLE_PRODUCT = {
    "asin": "B0GMRB1KLM",
    "title": "Beyblade X Ridge Triceratops 9-80GN Booster Pack with Defense Type Takara Tomy Top",
    "price": "24,99 €",
}


def build_rows() -> list[dict]:
    import datetime

    today = datetime.date.today()
    rows = []
    for row in SAMPLE_ROWS:
        config = MARKETPLACES[row["market"]]
        rows.append(
            {
                **row,
                "flag": config["flag"],
                "domain": config["domain"],
                "name": SAMPLE_NAME,
                "sort_key": (
                    datetime.date(today.year, 8 if row["day"] == 12 else 9, row["day"])
                    if row["day"]
                    else None
                ),
            }
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--send-discord",
        action="store_true",
        help="Actually post to Discord instead of only printing",
    )
    args = parser.parse_args()

    if args.send_discord and not browser.DISCORD_WEBHOOK_URL:
        logger.error("DISCORD_WEBHOOK_URL is not set — nothing would be sent. Aborting.")
        sys.exit(1)

    delivery = alerts.format_delivery_alert(SAMPLE_ASIN, SAMPLE_NAME, build_rows())
    new_product = alerts.format_new_product_alert("de", MARKETPLACES["de"], SAMPLE_PRODUCT)

    for label, message in (("DELIVERY ALERT", delivery), ("NEW PRODUCT ALERT", new_product)):
        print(f"\n{'=' * 60}\n{label}\n{'=' * 60}\n{message}\n")

    if not args.send_discord:
        print("Nothing sent. Re-run with --send-discord to post these to Discord.")
        return

    notify(delivery, True)
    notify(new_product, True)
    logger.info("Both sample alerts posted.")


if __name__ == "__main__":
    main()
