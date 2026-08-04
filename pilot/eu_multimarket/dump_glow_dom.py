#!/usr/bin/env python3
"""Dump Amazon's delivery-location ("glow") modal DOM for one market.

Diagnostic helper, not part of a run. set_delivery_location() in
track_delivery_multi.py has to drive this modal, and its markup differs
by market and changes over time — so read it from a real page instead of
guessing at selectors.

Usage (from the repo root, with the pilot's venv active):

    python pilot/eu_multimarket/dump_glow_dom.py de
    python pilot/eu_multimarket/dump_glow_dom.py de --headed   # watch it

Prints, for the country/postcode picker: each candidate element's tag,
whether it's visible, how many options it holds, and its text — followed
by the modal's raw outerHTML (trimmed) so the real structure is visible.
Writes the full HTML to state/glow_<market>.html as well.
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from marketplaces import MARKETPLACES

from playwright.sync_api import sync_playwright

PILOT_DIR = Path(__file__).parent

CANDIDATES = [
    "#GLUXCountryList",
    "#GLUXCountryListDropdown",
    "#GLUXCountryListDropdown-announce",
    "#GLUXZipUpdateInput",
    "#GLUXZipUpdate",
    "#GLUXZipConfirm",
    "[name='glowDoneButton']",
    "#GLUXConfirmClose",
    "#a-popover-content-1",
    ".a-popover-wrapper",
    ".a-dropdown-item",
    "[role='listbox']",
]

DESCRIBE_JS = """el => ({
    tag: el.tagName.toLowerCase(),
    type: el.getAttribute('type'),
    cls: (el.getAttribute('class') || '').slice(0, 70),
    selects: el.tagName.toLowerCase() === 'select' ? el.options.length : 0,
    options: el.querySelectorAll('option').length,
    listitems: el.querySelectorAll('li').length,
    links: el.querySelectorAll('a').length,
    visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
    text: (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 90),
})"""


def describe(page, selector: str) -> None:
    loc = page.locator(selector)
    count = loc.count()
    if count == 0:
        print(f"  {selector:36} ABSENT")
        return
    info = loc.first.evaluate(DESCRIBE_JS)
    print(
        f"  {selector:36} n={count} <{info['tag']}"
        f"{' type=' + info['type'] if info['type'] else ''}>"
        f" visible={info['visible']}"
        f" select_options={info['selects']} option={info['options']}"
        f" li={info['listitems']} a={info['links']}"
    )
    if info["cls"]:
        print(f"  {'':36}   class={info['cls']!r}")
    if info["text"]:
        print(f"  {'':36}   text={info['text']!r}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("market", choices=list(MARKETPLACES))
    parser.add_argument("--headed", action="store_true", help="show the browser")
    args = parser.parse_args()

    config = MARKETPLACES[args.market]
    url = f"https://www.{config['domain']}/-/en/"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed, channel="chromium")
        page = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale=config["locale"],
        ).new_page()

        print(f"navigating to {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)

        cookie = page.locator("#sp-cc-accept")
        if cookie.count():
            try:
                cookie.first.click(timeout=5000)
            except Exception:
                cookie.first.evaluate("el => el.click()")
            page.wait_for_timeout(1000)
            print("cookie banner accepted")

        line2 = page.locator("#glow-ingress-line2")
        print(f"#glow-ingress-line2 currently: "
              f"{line2.first.inner_text().strip()!r}" if line2.count() else "no #glow-ingress-line2")

        opened = False
        for opener in ("#nav-global-location-popover-link", "#glow-ingress-block", "#glow-ingress-line2"):
            loc = page.locator(opener)
            if loc.count():
                loc.first.click(timeout=5000)
                print(f"opened picker via {opener}")
                opened = True
                break
        if not opened:
            print("!! no location picker found on this page")
            browser.close()
            return

        page.wait_for_timeout(2500)

        print("\n=== candidate elements, BEFORE touching the dropdown ===")
        for selector in CANDIDATES:
            describe(page, selector)

        # If there's a styled dropdown button, click it and re-describe:
        # Amazon renders the option list into a popover elsewhere in the
        # DOM only once it's opened.
        dropdown = page.locator("#GLUXCountryListDropdown")
        if dropdown.count():
            try:
                dropdown.first.click(timeout=5000)
                page.wait_for_timeout(1500)
                print("\n=== candidate elements, AFTER clicking #GLUXCountryListDropdown ===")
                for selector in CANDIDATES:
                    describe(page, selector)
            except Exception as e:
                print(f"\n!! clicking #GLUXCountryListDropdown failed: {e}")

        # Any <select> anywhere in the page whose id/name mentions GLUX.
        print("\n=== every <select> with a GLUX-ish id/name ===")
        selects = page.locator("select")
        for i in range(selects.count()):
            info = selects.nth(i).evaluate(
                "el => ({id: el.id, name: el.name, n: el.options.length,"
                " opts: Array.from(el.options).slice(0, 8).map(o => o.text.trim())})"
            )
            if "glux" in (info["id"] + info["name"]).lower():
                print(f"  <select id={info['id']!r} name={info['name']!r}> {info['n']} options")
                print(f"     first few: {info['opts']}")

        out = PILOT_DIR / "state" / f"glow_{args.market}.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        html = page.content()
        out.write_text(html, encoding="utf-8")
        print(f"\nfull page HTML -> {out}  ({len(html):,} bytes)")

        # The modal markup itself, which is the bit worth pasting back.
        match = re.search(r'<div[^>]*id="a-popover-\d+".*?</div>\s*</div>\s*</div>', html, re.S)
        if match:
            snippet = match.group()[:6000]
            print(f"\n=== popover markup (first {len(snippet)} chars) ===\n{snippet}")
        else:
            print("\n(no a-popover-N container matched; see the saved HTML)")

        browser.close()


if __name__ == "__main__":
    main()
