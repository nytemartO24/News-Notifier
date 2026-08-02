# News-Notifier

Personal Discord-notification scrapers: Hypixel SkyBlock patch notes, and
Amazon.se Beyblade X (Hasbro toy) catalog/delivery tracking. Originally ran
entirely on GitHub Actions; production now runs on a VPS via cron (see
"VPS deployment" below) because Actions' scheduler proved unreliable well
below the ~30 min cadence needed. There's also a separate, unrelated-to-
production EU multi-market pilot (see below) exploring tracking the same
kind of Amazon data across all EU marketplaces, not just `.se`.

## Repo layout

```
scripts/                          Production scrapers (still exist; GH Actions
  scrape_hypixel.py                 schedules removed, workflow_dispatch-only
  scrape_hasbro_catalog.py          fallback kept — see "VPS deployment")
  track_delivery_date_playwright.py
  products.txt / blacklist.txt / delivery_state.json / errors.log
state.json                        Hypixel scraper's seen-thread state

deploy/                           VPS cron deployment (setup.sh, run.sh,
                                    crontab.txt, README.md, logrotate.conf)

pilot/eu_multimarket/             Standalone EU multi-market pilot — see below

.github/workflows/
  hypixel-scraper.yml             workflow_dispatch only (schedule removed)
  bbx-delivery-check.yml          workflow_dispatch only (schedule removed)
  new-bbx-products.yml            workflow_dispatch only (schedule removed)
  pilot-eu-multimarket-test.yml   workflow_dispatch only — on-demand test
                                    runner for the pilot, see below
```

## Production system (VPS, not GitHub Actions)

All three scrapers now run via cron on the user's own VPS —
`deploy/setup.sh` bootstraps it (clones repo, venv, Playwright, crontab from
`deploy/crontab.txt`), `deploy/run.sh` is the cron entry point (loads `.env`,
sets `RUN_ONCE=true`). Full details in `deploy/README.md`. State files
(`state.json`, `scripts/delivery_state.json`, `scripts/products.txt`) live
locally on the VPS, not committed back to git from there.

The GitHub Actions workflows for these three scrapers still exist but have
**no `schedule:` trigger** — `workflow_dispatch` only, kept as a manual
fallback. Do not re-add a schedule without also disabling the VPS cron job,
or you get duplicate Discord notifications and diverging state.

**`scripts/track_delivery_date_playwright.py` blacklist semantics**:
`blacklist.txt` excludes an ASIN from delivery tracking entirely (it started
as a literal copy of `products.txt` — nearly everything is blacklisted by
design; un-blacklist by commenting out a whole line with `#` to observe a
specific item again).

**Known-fixed bug**: `fetch_delivery_date()`'s fallback used to run the date
regex against the *entire page text* when no delivery-block CSS selector
matched — this occasionally matched unrelated dates elsewhere on the page
(e.g. a review's "Reviewed on 18 February 2026") and, since those carry an
explicit year, the "assume next year if already passed" correction never
applied, so stale/wrong dates could silently pass through as real. Fixed:
the date pattern is only searched within a genuinely matched selector's
text now; no selector match → straight to out-of-stock/no-date signal
checks → `UNKNOWN`. Also: any *already-past* date found in stored state is
now discarded on load (a real Amazon estimate can never be in the past, so
a past date in state is proof of old corruption, not a value worth
protecting).

## EU multi-market pilot (`pilot/eu_multimarket/`)

Standalone, **not deployed anywhere**, run manually (locally or via the
on-demand GitHub Actions workflow below) — nothing here touches `scripts/`
or production state. Covers every EU Amazon marketplace except UK/Ireland
(shipping-cost grounds): `se`, `de`, `fr`, `es`, `nl`, `be`, `it`, `pl` (see
`marketplaces.py`).

**Current model (went through several redesigns — this is the final one):**
a single hand-maintained `state/whitelist.txt` (one ASIN per line, `#`
comment-prefix pauses tracking without losing it). No catalog scraping, no
search, no discovery, no blacklist — `track_delivery_multi.py` navigates
directly to `https://www.<domain>/-/en/dp/<asin>` (Amazon's language-
override path segment) for every whitelisted ASIN, on every market named on
the command line, regardless of which market it was originally found on.
Earlier iterations had per-market `products.txt`, then a shared/global
`products.txt` + `blacklist.txt` pair with auto-discovery — all removed as
overcomplicated for what's actually needed (tracking a small, specific set
of products). Don't resurrect that complexity without being asked.

Also detects **Amazon vs. third-party seller** per listing
(`fetch_seller_info`/`is_amazon_seller`, targeting
`#merchantInfoFeature_feature_div` — confirmed correct against real HTML
from both an Amazon-sold and a Skydigital-sold listing) so you can tell a
fair Amazon-set price from a marketplace/scalper price.

**Known reliability issue, actively being debugged**: a full 8-market ×
8-ASIN GitHub Actions test run (see below) found only `.se` reliably
returns real delivery dates (5/8) with correct seller detection (5/5 of
those hits). Every other market got 0-1/8 real matches; `de`/`es`/`it`/`pl`
got zero. No crashes, no bot-blocking, no CAPTCHAs anywhere — homepage
warm-up and interstitial dismissal succeeded on every market. Root cause
(confirmed via a real `.de` product page's raw server HTML, provided by the
user): the delivery/seller info blocks are injected **client-side via JS
after `domcontentloaded`** on at least some markets, and aren't present in
the initial server HTML at all — even though it's a completely normal,
purchasable listing (Add-to-Cart, buybox all present). A fixed 2-second
sleep isn't always enough (even `.se` had 3/8 `UNKNOWN` in that same run).
Fix in progress: `fetch_delivery_date()` now does an adaptive
`page.wait_for_selector()` (up to 6s) for any target selector to appear
before reading page content, instead of relying solely on the fixed sleep.
**Not yet re-verified** — next step is rerunning the GitHub Actions test
workflow and confirming the hit rate improves, especially on `de`/`es`/
`it`/`pl`.

Locale-specific `no_date_signals`/`out_of_stock_signals` phrase lists in
`marketplaces.py` are still best-effort translations for every market
except `se`, unverified beyond whatever's been checked in this session.

### On-demand GitHub Actions test runner

`.github/workflows/pilot-eu-multimarket-test.yml` — `workflow_dispatch`
only, inputs are `asins` (comma-separated), `markets` (space-separated),
`send_discord` (bool, default false). Exists because outbound network
access to Amazon is often blocked at the proxy/policy level in sandboxed
dev environments (confirmed in this session: `curl` to `amazon.de` got a
403 org-policy denial), while GitHub's hosted runners have normal internet
access. Never commits anything back to the repo; uploads
`pilot/eu_multimarket/state/` (debug HTML, `delivery_state.json`) as a
7-day artifact — but that artifact is hosted on Azure Blob Storage, which
tends to be blocked by the same kind of restrictive proxy, so downloading
it programmatically from a sandboxed session may not work either. Pulling
results from the job log text (via GitHub's Actions API) works reliably;
inspecting a specific `debug_*.html` may require the user to download it
via the Actions UI and paste the content or a file directly.

Triggering this workflow (or any workflow) via the API requires the
GitHub App/integration to have Actions **write** access, not just read —
this had to be explicitly granted by the user mid-session (was a 403
before that, read-only after: could list runs/jobs/logs but not dispatch).
If a future session gets a 403 on `run_workflow`, that's very likely a
regression in that grant, not a code issue — ask the user to check.

## Working conventions established in this repo's sessions

- **PR-per-logical-change, not one big PR.** Each fix/feature gets its own
  commit(s), pushed, PR opened, and the user merges promptly (often within
  the same conversation turn or two). Because of that fast merge cadence:
  **before starting new work, always check whether the previous PR already
  merged** (`git fetch origin main`, compare to current branch). If it has,
  restart the designated branch from fresh `main`
  (`git checkout -B <branch> origin/main`) before committing more — don't
  stack new commits on already-merged history. If there are uncommitted
  changes in progress when this happens, `git stash push -u` before the
  checkout, then `git stash pop` after.
- Designated working branch: `claude/news-notifier-scraper-review-b7u8bu`.
- Commit messages and PR descriptions in this repo's history are detailed
  and explain *why*, not just *what* — matches the level of detail in
  `git log` here; keep doing that.
- The user runs commands directly on their VPS and pastes back real
  terminal output/logs/HTML when something needs verifying — that's the
  primary feedback loop for anything this session's sandbox can't verify
  itself (which is most live-Amazon-page behavior; see network
  restrictions above). Treat pasted real output as ground truth over any
  assumption.
