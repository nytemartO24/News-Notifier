# EU multi-market pilot (local, not deployed)

Validates scraping Beyblade X across multiple Amazon EU marketplaces
before deciding whether/how to roll it into the production scripts or
the VPS. **Run this on your own machine, manually — it is not wired into
the VPS cron, and nothing here touches `scripts/` or its state files.**

Covers `se` (existing production locale, known-good baseline), `de`, and
`fr` — see `marketplaces.py` for the per-market config.

## Known unknowns (why this is a pilot and not a rollout)

- `de`/`fr` `no_date_signals` and `out_of_stock_signals` in
  `marketplaces.py` are **guessed** phrasings, not confirmed against real
  Amazon.de/.fr pages. Expect `UNKNOWN` results and
  `state/<market>/debug_*.html` dumps on first runs — read those dumps
  and correct the phrase lists to match what's actually on the page.
- The `p_89:Hasbro` brand-filter refinement ID is copied from the `.se`
  search URL and **unverified** on `.de`/`.fr` — if the search page comes
  back with zero or clearly-wrong results, drop `rh=` from the search URL
  in `scrape_catalog_multi.py` and filter by title text instead.
- Date parsing (`build_date_pattern`) assumes German/French dates look
  like `21. Januar 2027` / `21 janvier 2027` — verify against real pages.

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
  state/<market>/
    products.txt          # discovered ASINs for that market
    hasbro_catalog.txt     # full scrape dump, overwritten each run
    delivery_state.json    # last known delivery date per ASIN
    debug_*.html           # saved on unrecognized page layouts
  logs/
    catalog_multi.log       # timestamped, mirrors console output
    delivery_multi.log
```

Both log files use the same START/END + exit-code bracketing as the VPS
cron logs (see the main `deploy/README.md`), so a quiet run is still
distinguishable from a run that never happened.

## Next steps once this looks solid

- Correct the `de`/`fr` phrase lists and brand filter based on real
  output.
- Decide whether to fold these into the production scripts (parameterize
  `scripts/scrape_hasbro_catalog.py` /
  `scripts/track_delivery_date_playwright.py` by marketplace) or keep
  them as a separate pipeline.
- Only then consider adding markets to the VPS crontab, and at a lower
  frequency than the .se jobs given the added per-marketplace bot-block
  risk discussed when this was scoped.
