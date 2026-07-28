# Running on a VPS instead of GitHub Actions

This moves the three scrapers off GitHub Actions' scheduler (unreliable
below ~1hr intervals, see the workflow history) onto plain cron on a small
VPS, running as often as every 30 minutes.

State files (`state.json`, `scripts/delivery_state.json`,
`scripts/products.txt`) live locally on the VPS from now on — they are no
longer committed back to the GitHub repo. Don't run the GitHub Actions
workflows on a schedule at the same time as this setup, or you'll get
duplicate Discord notifications and the VPS's and repo's state will drift
apart. The workflows' `schedule:` triggers have been removed for this
reason; `workflow_dispatch` (the manual "Run workflow" button) still works
as a one-off fallback if you ever need it.

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

## Adjusting the schedule

Edit `deploy/crontab.txt` (the `__APP_DIR__` placeholder gets substituted
in by `setup.sh` on each run), or just run `crontab -e` directly on the box
for a one-off tweak — note a manual `crontab -e` edit will be overwritten
the next time `setup.sh` runs.
