# EU multi-market pilot (local, not deployed)

Validates scraping Beyblade X across multiple Amazon EU marketplaces
before deciding whether/how to roll it into the production scripts or
the VPS. **Run this on your own machine, manually — it is not wired into
the VPS cron, and nothing here touches `scripts/` or its state files.**

Covers every EU Amazon marketplace except the UK and Ireland (excluded
on shipping-cost grounds): `se` (existing production locale, known-good
baseline), `de`, `fr`, `es`, `nl`, `be`, `it`, `pl` — see
`marketplaces.py` for the per-market config.

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
  **every page** and seeds BOTH `state/blacklist.txt` AND that market's
  own `products.txt` with every ASIN found — mirroring production, where
  `blacklist.txt` started life as a literal copy of `products.txt`.
  Nothing gets notified on this baseline pass. Each market still gets its
  own baseline scan even once the shared blacklist exists, since one
  market can carry ASINs another market's catalog doesn't.
- **Blacklist vs. products.txt are two different concerns**:
  `state/blacklist.txt` means "ignore this ASIN everywhere, including
  delivery tracking" (`track_delivery_multi.py` excludes any blacklisted
  ASIN from tracking even if it's still listed in a market's
  `products.txt`). `products.txt` means "this ASIN is known/trackable."
  Seeding both during the baseline is what makes the next point work.
- **To resume tracking a specific item** — including one from the
  original baseline — comment out its **entire line** in
  `state/blacklist.txt` (prefix with `#`, not just append a trailing
  comment; a fully-commented line contributes no ASIN to the blacklist
  set). Since it's already in `products.txt` from the baseline, it
  silently resumes being delivery-tracked on the next `track_delivery_multi.py`
  run — no re-notification, since it isn't "new," just no-longer-excluded.
- **Every later catalog run for an already-baselined market**: scans
  **only page 1** — since results are sorted newest-first, that's enough
  to catch genuinely new arrivals. Anything found there that's already in
  the shared blacklist or that market's `products.txt` is old news.
  Anything in neither is a real new arrival — that gets logged/notified
  once and appended to `products.txt`, deliberately **not** blacklisted
  (blacklisting it would immediately stop it from being delivery-tracked,
  defeating the point) — `products.txt` membership alone is enough to
  stop it being re-notified as "new" again.

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
- `p_123:219753` (Hasbro's brand id) is believed constant across the EU
  unified catalog, but hasn't been independently confirmed on every
  marketplace here — if a market's results look wrong or empty, check
  that first.
- Date parsing (`build_date_pattern`) assumes each locale's dates look
  like `21. Januar 2027` / `21 janvier 2027` / etc. — verify against
  real pages per market.
- Since the search URL forces `language=en`, it's worth checking whether
  the same trick works on product pages too (`/dp/<asin>?language=en`) —
  if so, the delivery tracker could drop its per-locale phrase/month
  guessing entirely and just reuse the English signal lists everywhere.
  Not done yet; flagging it as the most promising simplification given
  how many locales' phrasing is now just a guess.

## Setup

```bash
pip install -r pilot/eu_multimarket/requirements.txt
python -m playwright install chromium
```

## Usage

```bash
# Catalog discovery — defaults to ALL configured markets (se, de, fr,
# es, nl, be, it, pl) if you don't list any
python pilot/eu_multimarket/scrape_catalog_multi.py
python pilot/eu_multimarket/scrape_catalog_multi.py es it   # just these two

# Delivery-date check — reads state/<market>/products.txt written above
python pilot/eu_multimarket/track_delivery_multi.py es it
```

Running with no market arguments does a **full baseline scan of every
market that doesn't have one yet, in one go** — expect that first
combined run to take noticeably longer (8 markets × up to 10 pages each)
and to hit Amazon from your IP across every one of its EU properties in
a short window. Consider running markets a few at a time the first time
instead of all 8 at once, especially since several of these phrase lists
are unverified and might need a debug/tune cycle per market anyway.

Both scripts **default to a Discord dry-run** — they log what they would
send instead of actually posting, so pilot noise doesn't hit your real
channel. Pass `--send-discord` once you trust the output enough to want
real notifications, using the same `DISCORD_WEBHOOK_URL` /
`DISCORD_USER_ID` env vars as production (source your `.env`, or export
them directly).

`track_delivery_multi.py` logs every step per product — navigation,
interstitial handling, which selector/signal matched, elapsed time — so
a run that looks stuck isn't a black box: `tail -f
logs/delivery_multi.log` shows exactly which market/ASIN/step it's on.
If a market genuinely seems to hang, set `HEADLESS=false` to watch the
actual browser:

```bash
HEADLESS=false python pilot/eu_multimarket/track_delivery_multi.py de
```

If a market's `products.txt` was seeded by hand from a full catalog dump
(e.g. copy-pasted from `hasbro_catalog.txt`) rather than built up through
the normal new-arrival flow, expect most of those entries to now get
excluded as blacklisted the moment you rerun the delivery tracker — that
overlap with the baseline scan's blacklist entries is expected, not a
bug; `load_products()` logs how many got skipped for exactly this
reason.

**If a market was already baselined before this fix** (its
`.baseline_done` marker already exists), its `products.txt` won't get
backfilled automatically — the baseline-seeds-both-files behavior only
applies the first time a market is scanned. To retroactively get the
"comment out of blacklist.txt to resume tracking" behavior working for
an already-baselined market, delete its marker and rerun the catalog
scraper for just that market:

```bash
rm pilot/eu_multimarket/state/<market>/.baseline_done
python pilot/eu_multimarket/scrape_catalog_multi.py <market>
```

This redoes the full all-pages scan and merges everything into both
files (it won't duplicate what's already there, or re-notify anything).

## Where things land

```
pilot/eu_multimarket/
  state/
    blacklist.txt            # SHARED across all markets — baseline scan results + anything you deliberately blacklist by hand
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

- Correct each market's phrase lists based on real output; confirm the
  brand id actually returns Hasbro-only results on each market.
- Decide whether to fold these into the production scripts (parameterize
  `scripts/scrape_hasbro_catalog.py` /
  `scripts/track_delivery_date_playwright.py` by marketplace, and
  consider adopting the same blacklist-seeding + page-1-only approach
  there) or keep them as a separate pipeline.
- Only then consider adding markets to the VPS crontab, and at a lower
  frequency than the .se jobs given the added per-marketplace bot-block
  risk discussed when this was scoped.
