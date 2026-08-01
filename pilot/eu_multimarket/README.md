# EU multi-market pilot (local, not deployed)

Tracks Amazon delivery dates for a hand-picked list of ASINs across
multiple EU marketplaces at once. **Run this on your own machine,
manually — it is not wired into the VPS cron, and nothing here touches
`scripts/` or its state files.**

Covers every EU Amazon marketplace except the UK and Ireland (excluded
on shipping-cost grounds): `se` (existing production locale, known-good
baseline), `de`, `fr`, `es`, `nl`, `be`, `it`, `pl` — see
`marketplaces.py` for the per-market config.

## The model: whitelist in, direct navigation out

There is no catalog scraping, no search, no discovery, no blacklist.
Just `state/whitelist.txt` — one ASIN per line, maintained by hand — and
`track_delivery_multi.py`, which for every ASIN in that file:

1. Navigates directly to `https://www.<domain>/dp/<asin>` — no search
   step at all.
2. Does this **on every market named on the command line**, since ASINs
   are the same product across Amazon's EU marketplaces (a delivery date
   or "out of stock" status is genuinely different per country, even for
   the same product, so it's worth checking everywhere).
3. Alerts (Discord, or logged in dry-run) only when a real delivery date
   newly appears or moves earlier — same logic as production.
4. Also extracts the buybox's "Sold by" / "Dispatches from" info and
   flags whether the listing is actually sold by Amazon, or by a
   third-party seller (possible scalper pricing) — logged per product,
   included in Discord alerts, and flagged with `⚠️ NOT AMAZON` in the
   summary table.

To add a product: find its ASIN (from the product URL,
`amazon.se/dp/<ASIN>`) and add a line to `state/whitelist.txt`. To pause
tracking one without losing it, comment out its whole line (`#` at the
very start). To drop it for good, delete the line.

```bash
echo "B0G4NF8QDZ  # Beyblade X Curse Mummy 7-55W UX Booster Pack" >> pilot/eu_multimarket/state/whitelist.txt
```

If `state/whitelist.txt` doesn't exist yet, the script creates it with
an example line and exits — edit it and rerun.

## Known unknowns (why this is a pilot and not a rollout)

- Every market except `se`'s `no_date_signals` and `out_of_stock_signals`
  in `marketplaces.py` are **guessed** phrasings translated from the
  English list, not confirmed against real Amazon pages in that locale.
  Expect `UNKNOWN` results and `state/<market>/debug_*.html` dumps on
  first runs — read those dumps and correct the phrase lists to match
  what's actually on the page.
- Belgium (`be`) is bilingual (Dutch/French) but only has the Dutch
  phrase list so far — if French-language listings show up as
  `UNKNOWN`, that's why; add French phrases to `marketplaces["be"]`
  once you see it happen.
- Date parsing (`build_date_pattern`) assumes each locale's dates look
  like `21. Januar 2027` / `21 janvier 2027` / etc. — verify against
  real pages per market.
- Seller detection targets `#merchantInfoFeature_feature_div`, confirmed
  directly against real HTML from both an Amazon-sold listing and a
  third-party one (Skydigital) — it consistently holds the actual seller
  name regardless of label wording ("Shipper / Seller" for Amazon vs.
  "Sold by" for third parties). `SELLER_FALLBACK_SELECTORS` (the earlier
  guesses: `#merchant-info`, `#tabular-buybox`, etc.) are kept only in
  case some market's layout genuinely differs — those remain unverified.

## Setup

```bash
pip install -r pilot/eu_multimarket/requirements.txt
python -m playwright install chromium
```

## Usage

```bash
# Checks every ASIN in state/whitelist.txt against every configured
# market by default
python pilot/eu_multimarket/track_delivery_multi.py

# Or just specific markets
python pilot/eu_multimarket/track_delivery_multi.py se de fr
```

Defaults to a **Discord dry-run** — it logs what it would send instead
of actually posting, so pilot noise doesn't hit your real channel. Pass
`--send-discord` once you trust the output enough to want real
notifications, using the same `DISCORD_WEBHOOK_URL` / `DISCORD_USER_ID`
env vars as production (source your `.env`, or export them directly).

Logs every step per product — navigation, interstitial handling, which
selector/signal matched, elapsed time — so a run that looks stuck isn't
a black box: `tail -f logs/delivery_multi.log` shows exactly which
market/ASIN/step it's on. If a market genuinely seems to hang, set
`HEADLESS=false` to watch the actual browser:

```bash
HEADLESS=false python pilot/eu_multimarket/track_delivery_multi.py de
```

## Migrating from the old catalog-discovery/blacklist model

If you were running an earlier version of this pilot, you'll have
`state/products.txt`, `state/blacklist.txt`, per-market `.baseline_done`
markers, and `hasbro_catalog.txt` dumps left over — none of that is read
by anything anymore. Seed your new whitelist from whatever you'd
already curated as "actually tracked" (`products.txt` minus
`blacklist.txt`), then clean up the rest:

```bash
cd pilot/eu_multimarket/state
comm -23 <(sort -u products.txt) <(sort -u blacklist.txt) > whitelist.txt
rm -f products.txt blacklist.txt */.baseline_done */hasbro_catalog.txt
```

Check `whitelist.txt` afterward and trim it down to what you actually
want tracked — that `comm` command carries over everything you'd
un-blacklisted, which may be more than you want going forward now that
there's no discovery step re-populating it.

## Where things land

```
pilot/eu_multimarket/
  state/
    whitelist.txt             # hand-maintained, shared across all markets
    <market>/
      delivery_state.json      # last known delivery date per ASIN, this market
      debug_*.html              # saved on unrecognized page layouts
  logs/
    delivery_multi.log
```

The log uses the same START/END + exit-code bracketing as the VPS cron
logs (see the main `deploy/README.md`), so a quiet run is still
distinguishable from a run that never happened.

## Next steps once this looks solid

- Correct each market's phrase lists based on real output.
- Decide whether to fold this into
  `scripts/track_delivery_date_playwright.py` (parameterize it by
  marketplace, checking the same ASIN across all of them) or keep it as
  a separate pipeline.
- Only then consider adding markets to the VPS crontab, at whatever
  cadence makes sense given the per-marketplace bot-block risk discussed
  when this was scoped.
