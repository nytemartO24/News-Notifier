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

## Page language — read this before touching `marketplaces.py`

Product URLs use Amazon's `/-/en/` language-override path segment, so
**the rendered page text is English, not the market's native language.**
For a long time every market except `se` declared only native month
names and native signal phrases, which meant nothing could match on an
English page — and `se` was the one market whose tables already held the
English names. That is the single biggest reason the non-`se` markets
were returning `UNKNOWN`. Measured against English delivery text, the
old per-market month tables could match:

| market | months matchable | observed hit rate at the time |
|---|---|---|
| `se` | 12/12 | the only market that worked |
| `de` | 6/12 (`Januar`≈`January`, `April`, `August`, …) | 0/8 |
| `nl` / `be` | 4/12 | 0-1/8 |
| `fr` / `es` / `it` / `pl` | 0/12 | 0/8 |

Every market now gets the **union** of shared English tables and its own
native ones, and `locale` is pinned to `en-<CC>` so the Accept-Language
header agrees with the URL instead of fighting it. Whichever language
Amazon actually serves, it parses. If you add a market, add its native
tables — the English ones are merged in for you by `_merge()`.

`de` at 6/12 does not fully explain its 0/8, so at least one other
factor was in play there (consistent with the real `.de` server HTML
showing no delivery block at all — see the client-side-injection note in
the root `CLAUDE.md`). The adaptive `wait_for_selector` and the cookie
banner dismissal are the mitigations for that half; they still need a
real run to confirm.

## Known unknowns (why this is a pilot and not a rollout)

- Native-language `no_date_signals` / `out_of_stock_signals` in
  `marketplaces.py` are still **guessed** translations, unconfirmed
  against real pages in those locales. They now sit behind the verified
  English phrasing as a fallback rather than being the only thing
  standing between a page and `UNKNOWN`, so a bad guess is much less
  costly than it was — but read `state/<market>/debug_*.html` dumps and
  correct them when you see them.
- Belgium (`be`) is bilingual, so it carries both Dutch and French month
  names and phrase lists.
- Date parsing handles day-first (`21 January`, `21. Januar`,
  `15 de agosto`, `12 sierpnia`), month-first (`January 21, 2027`),
  abbreviations (`12 Aug.`), and ordinals (`1st`/`1er`/`1°`). Polish is
  listed in both genitive (`sierpnia` — the form dates actually use) and
  nominative; the nominative-only table it had before could not match a
  single real Polish date.
- Extracted dates are sanity-checked against a 0..400-day window
  (`MAX_DELIVERY_HORIZON_DAYS`) before being trusted, and signal phrases
  are matched only inside the availability/buybox region
  (`AVAILABILITY_SELECTORS`) rather than the whole page — a whole-page
  search for `release date` matches the product-details table on
  essentially every toy listing, which turned "delivery block hasn't
  rendered" into a confident, wrong `NO DATE YET`.
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
