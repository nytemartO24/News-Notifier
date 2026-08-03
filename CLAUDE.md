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

**Page language — the big one, fixed and now verified live**: product
URLs use the `/-/en/` override, so pages render in **English**, but every
market except `se` declared only *native* month names and signal phrases
in `marketplaces.py` — so nothing could match. `se` was the sole market
whose tables already contained English, which is exactly why `se` was the
only market that worked. Measured against English delivery text, the old
tables could match 12/12 months for `se`, 6/12 for `de`, 4/12 for
`nl`/`be`, and **0/12 for `fr`/`es`/`it`/`pl`** — which tracks the
observed hit rates closely. Fixed by merging shared English tables into
every market (`_merge()` in `marketplaces.py`) and pinning `locale` to
`en-<CC>` so Accept-Language stops fighting the URL. (`de`'s 6/12 didn't
fully explain its 0/8 — the live run identified the missing factor as
the international-shopping artefact described below, not a parsing
problem.) Don't "simplify" a market back down to native-only tables:
`pl` proves the native side still matters, since Poland ignores the
`/-/en/` override and serves `pl-pl`.

Also fixed alongside it, all in the same direction (fewer confident-but-wrong
results): date parsing now handles month-first order, abbreviations,
ordinals, the Spanish `15 de agosto` connector and Polish genitive month
forms (`sierpnia`, not `sierpień` — dates never use the nominative);
extracted dates are range-checked (0..400 days) before being stored or
alerted on; signal phrases are matched only within the availability/buybox
region instead of the whole page (a whole-page `release date` match hits
the product-details table of essentially every toy listing, turning "the
delivery block hasn't rendered" into a confident wrong `NO DATE YET`); and
the EU cookie-consent banner (`#sp-cc-accept`) is dismissed, which
production never had to handle because its VPS browser profile is warm.

**Verified live** (run 30803204143, 2026-08-03, 8 markets x 8 ASINs,
`--debug`). The language fix is confirmed correct by direct evidence:
`<html lang>` came back **`en-gb` on every market except `pl`** (which
served `pl-pl`, ignoring the `/-/en/` override) — so the old
native-only tables genuinely could not have matched, and `pl`'s hits
came via the genitive Polish months added alongside. Hit rates went
from `se` 5/8 and everything else 0-1/8, to:

| market | real dates | unresolved (UNKNOWN) |
|---|---|---|
| `se` | 6/8 | 0 |
| `nl` | 6/8 | 0 |
| `de` | 3/8 | 4 — all the international-shopping artefact below |
| `pl` | 3/8 | 0 |
| `be` | 3/8 | 2 |
| `fr` / `es` / `it` | 2/8 | 2 / 1 / 2 |

Every market now returns real dates. Most non-hits are legitimate
states, not failures: 53 of 64 pages produced a definite verdict
(`currently unavailable` -> NO DATE YET x11, `temporarily out of stock`
x8, and `obecnie niedostepny` x5 — a *guessed* Polish phrase that turned
out correct). Zero implausible-date rejections; the cookie banner was
accepted once per market (production never had to handle it); the
`[data-csa-c-content-id="DEXUnifiedCXPDM"]` selector added in the same
change is doing real work, including on the Polish pages.

**`amazon.de` + a non-EU host = international-shopping mode.** All 8
`.de` pages carried an "International Shopping Transition Alert ... items
that dispatch to United States" banner, with no add-to-cart, no price
block, an empty `#availability`, and — on the three that did render a
delivery block — prices and dates in **USD for shipping to the US**.
Those are real dates describing the wrong thing. `fetch_delivery_date()`
now detects the banner and returns UNKNOWN rather than storing a date
that would become the baseline for a bogus "moved earlier" alert. This
is an artefact of *where the run happens*, not a bug: it should not
occur from the European VPS. It is detected rather than worked around,
because faking a delivery location is fragile and would hide the fact
that a run's numbers aren't comparable to a local one's. **A GitHub
Actions run is therefore not a substitute for a run from an EU host,
especially for `de`.**

The earlier client-side-injection theory is not contradicted, but it
was never the main event: the remaining `de` failures are `ABSENT`
delivery blocks on offer-less international pages, not `PRESENT BUT
EMPTY` ones. The adaptive `wait_for_selector` stays.

Native-language `no_date_signals`/`out_of_stock_signals` in
`marketplaces.py` are still best-effort translations, but they now sit
behind verified English phrasing as a fallback rather than being the
only thing between a page and `UNKNOWN`. Only one has been confirmed to
fire against a real page so far: `pl`'s `obecnie niedostępny` (5 hits).

**Debug logging**: `--debug` / `DEBUG=true` (the test workflow's `debug`
input, default on) logs a full page snapshot per product — `<html lang>`
(did the `/-/en/` override win?), a page-kind matrix (product page vs.
homepage/CAPTCHA), a per-selector `ABSENT` / `PRESENT BUT EMPTY` /
text-found matrix (**`PRESENT BUT EMPTY` is the client-side-injection
signature** — wait longer, don't change the selector), plus
diagnostic-only whole-page scans showing every date-like string and the
page's actual delivery wording. Without `--debug` the same snapshot is
still emitted for every `UNKNOWN`. Per-market `OUTCOMES` tally and a
final `HIT RATE BY MARKET` scoreboard — that's the line to diff between
runs. All of this goes to the job log deliberately, because the artifact
usually can't be downloaded (see below).

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
