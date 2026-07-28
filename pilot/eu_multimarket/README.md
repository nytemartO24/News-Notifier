# EU multi-market pilot (local, not deployed)

Validates scraping Beyblade X across multiple Amazon EU marketplaces
before deciding whether/how to roll it into the production scripts or
the VPS. **Run this on your own machine, manually — it is not wired into
the VPS cron, and nothing here touches `scripts/` or its state files.**

Covers `se` (existing production locale, known-good baseline), `de`, and
`fr` — see `marketplaces.py` for the per-market config.

## How catalog discovery works

`scrape_catalog_multi.py` searches Hasbro's brand catalog directly,
sorted newest-first:

```
https://www.<domain>/s?k=beyblade+x&rh=p_123%3A219753&s=date-desc-rank&dc&language=en
```

Blacklist model (intentionally inverted from a normal allowlist), with
**one blacklist shared across all markets** (`state/blacklist.txt`) —
since ASINs are the same product across Amazon's EU marketplaces, an
item blacklisted after being seen on `.se` is recognized as already-known
on `.de`/`.fr` too, not re-flagged separately per market:

- **First run for a given market** (tracked per-market via a
  `state/<market>/.baseline_done` marker, independent of whether the
  shared blacklist already has entries from another market): scans
  **every page** and merges every ASIN found into `state/blacklist.txt`.
  Nothing gets notified on this baseline pass — the point is just to
  establish "everything that already exists" as not-interesting. Each
  market still gets its own baseline scan even once the shared blacklist
  exists, since one market can carry ASINs another market's catalog
  doesn't.
- **Every later run for an already-baselined market**: scans **only page
  1** — since results are sorted newest-first, that's enough to catch new
  arrivals. Anything found there that's already in `state/blacklist.txt`
  is ignored. Anything **not** in the shared blacklist gets
  logged/notified once, appended to that market's own `products.txt` (so
  `track_delivery_multi.py` can track its delivery date — prices/stock/
  dates still differ by market even for the same ASIN), and then
  re-added to `state/blacklist.txt` so it doesn't repeat next run **for
  any market**.
- **To be told about a specific item again** — including one you were
  already notified about, or one from the original baseline you
  actually care about — comment out its **entire line** in
  `state/blacklist.txt` (prefix with `#`, not just append a trailing
  comment). A fully-commented line contributes no ASIN to the blacklist
  set, so it'll show up as "new" again on the next run for whichever
  market it's found on.

## Known unknowns (why this is a pilot and not a rollout)

- `de`/`fr` `no_date_signals` and `out_of_stock_signals` in
  `marketplaces.py` are **guessed** phrasings, not confirmed against real
  Amazon.de/.fr pages. Expect `UNKNOWN` results and
  `state/<market>/debug_*.html` dumps on first runs — read those dumps
  and correct the phrase lists to match what's actually on the page.
- `p_123:219753` (Hasbro's brand id) is believed constant across the EU
  unified catalog, but hasn't been independently confirmed on every
  marketplace here — if a market's results look wrong or empty, check
  that first.
- Date parsing (`build_date_pattern`) assumes German/French dates look
  like `21. Januar 2027` / `21 janvier 2027` — verify against real pages.
- Since the search URL forces `language=en`, it's worth checking whether
  the same trick works on product pages too (`/dp/<asin>?language=en`) —
  if so, the delivery tracker could drop its per-locale phrase/month
  guessing entirely and just reuse the English signal lists everywhere.
  Not done yet; flagging it as the most promising simplification if the
  guessed `de`/`fr` phrasing above turns out unreliable.

## Setup

```bash
pip install -r pilot/eu_multimarket/requirements.txt
python -m playwright install chromium
```

## Usage

```bash
# Catalog discovery — defaults to all configured markets (se, de, fr)
python pilot/eu_multimarket/scrape_catalog_multi.py
python pilot/eu_multimarket/scrape_catalog_multi.py de fr   # just these two

# Delivery-date check — reads state/<market>/products.txt written above
python pilot/eu_multimarket/track_delivery_multi.py de fr
```

Both scripts **default to a Discord dry-run** — they log what they would
send instead of actually posting, so pilot noise doesn't hit your real
channel. Pass `--send-discord` once you trust the output enough to want
real notifications, using the same `DISCORD_WEBHOOK_URL` /
`DISCORD_USER_ID` env vars as production (source your `.env`, or export
them directly).

## Where things land

```
pilot/eu_multimarket/
  state/
    blacklist.txt            # SHARED across all markets — baseline + everything already notified about
    <market>/
      .baseline_done          # marker: has this market had its own full scan yet
      products.txt            # new arrivals found for this market, for delivery tracking
      hasbro_catalog.txt       # full scrape dump, overwritten each run
      delivery_state.json      # last known delivery date per ASIN, this market
      debug_*.html              # saved on unrecognized page layouts
  logs/
    catalog_multi.log       # timestamped, mirrors console output
    delivery_multi.log
```

Both log files use the same START/END + exit-code bracketing as the VPS
cron logs (see the main `deploy/README.md`), so a quiet run is still
distinguishable from a run that never happened.

## Next steps once this looks solid

- Correct the `de`/`fr` phrase lists based on real output; confirm the
  brand id actually returns Hasbro-only results on each market.
- Decide whether to fold these into the production scripts (parameterize
  `scripts/scrape_hasbro_catalog.py` /
  `scripts/track_delivery_date_playwright.py` by marketplace, and
  consider adopting the same blacklist-seeding + page-1-only approach
  there) or keep them as a separate pipeline.
- Only then consider adding markets to the VPS crontab, and at a lower
  frequency than the .se jobs given the added per-marketplace bot-block
  risk discussed when this was scoped.
