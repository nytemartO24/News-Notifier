# EU multi-market pilot (local, not deployed)

Validates scraping Beyblade X across multiple Amazon EU marketplaces
before deciding whether/how to roll it into the production scripts or
the VPS. **Run this on your own machine, manually — it is not wired into
the VPS cron, and nothing here touches `scripts/` or its state files.**

Covers every EU Amazon marketplace except the UK and Ireland (excluded
on shipping-cost grounds): `se` (existing production locale, known-good
baseline), `de`, `fr`, `es`, `nl`, `be`, `it`, `pl` — see
`marketplaces.py` for the per-market config.

## The core model: everything is global except delivery state

There is **one shared product list and one shared blacklist for all
markets combined** — `state/products.txt` and `state/blacklist.txt`.
There is deliberately **no per-market products.txt anymore.** ASINs are
the same product across Amazon's EU marketplaces, so "is this ASIN
trackable" is a single yes/no question, not one answer per market.

- `state/products.txt` = every ASIN ever discovered, on any market.
- `state/blacklist.txt` = which of those to ignore (not notify about,
  not delivery-track) — same file whether it was first seen on `.se` or
  `.pl`.
- **Tracked set** = `products.txt` minus `blacklist.txt`. Every ASIN in
  that set gets checked on **every market you run**
  (`track_delivery_multi.py se de fr ...`), regardless of which market it
  was originally discovered on. If a product is genuinely unavailable on
  a given domain, that's a real, useful "no listing" or error result —
  not a reason to skip checking it there.
- Only `delivery_state.json` (last known date) and `debug_*.html` dumps
  stay per-market under `state/<market>/`, since the same ASIN can
  legitimately have a different delivery date, or none at all, per
  country.

## How catalog discovery works

`scrape_catalog_multi.py` searches Hasbro's brand catalog directly,
sorted newest-first:

```
https://www.<domain>/s?k=beyblade+x&rh=p_123%3A219753&s=date-desc-rank&dc&language=en
```

- **First run for a given market** (tracked per-market via a
  `state/<market>/.baseline_done` marker): scans **every page** and seeds
  BOTH shared files with every ASIN found — mirroring production, where
  `blacklist.txt` started life as a literal copy of `products.txt`.
  Nothing gets notified on this baseline pass. Every market still gets
  its own baseline scan even once the shared files have entries from
  another market, since one market's search results can surface ASINs
  another market's search hasn't (yet).
- **To resume tracking a specific item** — including one from a
  baseline — comment out its **entire line** in `state/blacklist.txt`
  (prefix with `#`, not just a trailing comment; a fully-commented line
  contributes no ASIN to the blacklist set). Since it's already in the
  shared `products.txt`, it silently resumes being delivery-tracked **on
  every market you run** next time — no re-notification, since it isn't
  "new," just no-longer-excluded.
- **Every later catalog run for an already-baselined market**: scans
  **only page 1** — since results are sorted newest-first, that's enough
  to catch genuinely new arrivals. Anything found there that's already in
  either shared file is old news. Anything in neither is a real new
  arrival — logged/notified once and appended to the shared
  `products.txt`, deliberately **not** blacklisted (blacklisting it would
  immediately stop it from being delivery-tracked, defeating the point).
- Whether an ASIN needs adding to the blacklist during a baseline scan is
  decided against **every line ever recorded for it, commented or not**
  — not just currently-active ones. Checking only active entries would
  mean a later market's baseline scan sees a deliberately-un-blacklisted
  ASIN as "not yet blacklisted" and silently re-adds a duplicate active
  line for it, undoing the un-blacklisting. (This bit us once already —
  if you're cleaning up an old `blacklist.txt` from before this existed,
  check for an ASIN appearing on more than one line.)

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

# Delivery-date check — checks the SAME global tracked-ASIN list
# (state/products.txt minus state/blacklist.txt) against every market
# you list here
python pilot/eu_multimarket/track_delivery_multi.py se de fr es nl be it pl
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

## Migrating from the old per-market products.txt

If you were running an earlier version of this pilot, you'll have
leftover `state/<market>/products.txt` files that are no longer read by
anything — `track_delivery_multi.py` now only reads the single
`state/products.txt`. Consolidate them once:

```bash
cd pilot/eu_multimarket/state
cat */products.txt 2>/dev/null | sort -u -t'#' -k1,1 > /tmp/merged_products.txt
cat /tmp/merged_products.txt products.txt 2>/dev/null | sort -u -t'#' -k1,1 > products.txt.new
mv products.txt.new products.txt
rm */products.txt
```

That merges every market's old list plus whatever's already in the new
shared file, deduplicated by ASIN, then removes the now-unused per-market
copies. Check `products.txt` afterward — you'll likely want to trim it
down rather than track everything that was ever in any market's old
list; anything you don't want tracked, blacklist it (or just delete its
line from `products.txt` entirely, which works too — it's a plain
membership list, not a comment-toggle file like `blacklist.txt`).

## Where things land

```
pilot/eu_multimarket/
  state/
    blacklist.txt            # SHARED across all markets — baseline scan results + anything you deliberately blacklist by hand
    products.txt              # SHARED across all markets — every trackable ASIN
    <market>/
      .baseline_done          # marker: has this market had its own full scan yet
      hasbro_catalog.txt       # full scrape dump for this market, overwritten each run
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
  consider adopting the same global-list + page-1-only approach there)
  or keep them as a separate pipeline.
- Only then consider adding markets to the VPS crontab, and at a lower
  frequency than the .se jobs given the added per-marketplace bot-block
  risk discussed when this was scoped.
