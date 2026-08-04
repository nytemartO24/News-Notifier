# Running on a VPS instead of GitHub Actions

This moves the scrapers off GitHub Actions' scheduler (unreliable
below ~1hr intervals, see the workflow history) onto plain cron on a small
VPS, running as often as every 30 minutes.

**Three jobs run on cron:**

| every | job | what |
|---|---|---|
| `:00 :30` | `pilot/eu_multimarket/track_delivery_multi.py` | delivery dates for the whitelist, across se/de/fr/es |
| `:10 :40` | `pilot/eu_multimarket/scrape_catalog_multi.py` | new Beyblade X products, per market |
| `:20 :50` | `scripts/scrape_hypixel.py` | Hypixel patch notes |

Beyblade tracking is the **EU multi-market pilot**. It replaced the
`.se`-only `scripts/track_delivery_date_playwright.py` and
`scripts/scrape_hasbro_catalog.py`, which are no longer scheduled — they
remain in the repo, still runnable by hand, and are marked SUPERSEDED at
the top of each file. Don't schedule both: they track the same products
and would produce duplicate Discord alerts from diverging state.

Re-running `deploy/setup.sh` performs that migration — it reports which
retired jobs it removed, then installs the current schedule.

State files (`state.json`, `pilot/eu_multimarket/state/`,
`scripts/delivery_state.json`, `scripts/products.txt`) live locally on
the VPS from now on — they are no
longer committed back to the GitHub repo. Don't run the GitHub Actions
workflows on a schedule at the same time as this setup, or you'll get
duplicate Discord notifications and the VPS's and repo's state will drift
apart. The workflows' `schedule:` triggers have been removed for this
reason; `workflow_dispatch` (the manual "Run workflow" button) still works
as a one-off fallback if you ever need it.

## Upgrading an existing VPS

```bash
cd ~/news-notifier
git pull
./deploy/setup.sh      # idempotent: re-installs deps and the crontab
./deploy/status.sh     # shows what's scheduled and what has run
```

`setup.sh` never overwrites your `.env`. If you want the EU pilot to quote
delivery dates against somewhere other than the default (Sweden / 37116),
add `DELIVERY_COUNTRY` and `DELIVERY_POSTCODE` — see `deploy/env.example`.

## 1. Provision the VPS

Any small Ubuntu box works. For Hetzner:

1. Create a **CPX11** server (Ubuntu 24.04, ~€4.5/mo) at
   https://console.hetzner.cloud
2. Add your SSH key during creation, then `ssh root@<server-ip>`.
3. (Recommended) create a non-root user instead of running everything as
   root: `adduser deploy && usermod -aG sudo deploy`, then `su - deploy`
   for the rest of this guide.

## 2. Run the setup script

```bash
curl -fsSL https://raw.githubusercontent.com/nytemartO24/News-Notifier/main/deploy/setup.sh | bash
```

This clones the repo to `~/news-notifier`, creates a venv, installs Python
deps + Playwright's Chromium (with its OS-level libs), creates `.env` from
the template, and installs the crontab from `deploy/crontab.txt`.

## 3. Fill in secrets

```bash
nano ~/news-notifier/.env
```

Set `DISCORD_WEBHOOK_URL` and (optionally) `DISCORD_USER_ID` — same values
you had as GitHub Actions repo secrets.

## 4. Verify

```bash
crontab -l                              # confirm the three jobs are installed
tail -f ~/news-notifier/logs/delivery.log   # after the next :00/:30 tick
```

## Updating later

```bash
cd ~/news-notifier && git pull && ./deploy/setup.sh
```

Re-running `setup.sh` is safe — it reinstalls dependencies and refreshes
the crontab without touching your `.env`.

## Monitoring — is it actually running?

Each cron entry writes to its own log under `~/news-notifier/logs/`
(`delivery.log`, `catalog.log`, `hypixel.log`), and every invocation is
bracketed with a `=== <timestamp> START ...` / `=== <timestamp> END ...
(exit N)` marker regardless of what the script itself prints — so a log
with only `START`/`END` lines and no exit-code failures means it ran and
found nothing new, not that it never ran.

```bash
tail -n 50 ~/news-notifier/logs/delivery.log   # recent runs + their output
grep "exit 0" ~/news-notifier/logs/*.log | tail   # confirm recent runs succeeded
grep -v "exit 0" ~/news-notifier/logs/*.log | grep END   # any non-zero exits
```

To confirm cron itself is firing at all (separate question from whether
the script succeeded — a missing `START` line for an expected tick means
cron never launched it):

```bash
grep CRON /var/log/syslog | tail -20
```

Logs are rotated weekly (4 weeks kept, compressed) via
`/etc/logrotate.d/news-notifier`, installed automatically by `setup.sh`.

## Adjusting the schedule

Edit `deploy/crontab.txt` (the `__APP_DIR__` placeholder gets substituted
in by `setup.sh` on each run), or just run `crontab -e` directly on the box
for a one-off tweak — note a manual `crontab -e` edit will be overwritten
the next time `setup.sh` runs.
