#!/usr/bin/env bash
# Bootstraps this repo on a fresh Ubuntu VPS to run the three scrapers on
# cron instead of GitHub Actions. Idempotent — safe to re-run after a
# `git pull` to pick up dependency or crontab changes.
#
# Usage (as a non-root sudo-capable user):
#   curl -fsSL https://raw.githubusercontent.com/nytemartO24/News-Notifier/main/deploy/setup.sh | bash
# or, if you've already cloned the repo:
#   ./deploy/setup.sh
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/news-notifier}"
REPO_URL="${REPO_URL:-https://github.com/nytemartO24/News-Notifier.git}"

echo "==> Installing system packages"
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip git

echo "==> Cloning/updating repo at $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull
else
  git clone "$REPO_URL" "$APP_DIR"
fi

cd "$APP_DIR"
mkdir -p logs

echo "==> Creating virtualenv"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> Installing Python dependencies"
pip install --upgrade pip
pip install -r requirements-bbx.txt -r requirements-hypixel.txt \
  -r pilot/eu_multimarket/requirements.txt

echo "==> Installing Playwright's Chromium and its OS dependencies"
python -m playwright install-deps chromium
python -m playwright install chromium

if [ ! -f "$APP_DIR/.env" ]; then
  echo "==> Creating .env from template — edit this with your real secrets"
  cp deploy/env.example "$APP_DIR/.env"
fi

chmod +x deploy/run.sh deploy/status.sh

# Show what's being retired before it disappears, so a re-run that
# silently drops a job you still wanted is visible rather than mysterious.
retired="$(crontab -l 2>/dev/null \
  | grep "$APP_DIR/deploy/run.sh" \
  | grep -E "scripts/(track_delivery_date_playwright|scrape_hasbro_catalog)\.py" || true)"
if [ -n "$retired" ]; then
  echo "==> Removing superseded .se-only Beyblade cron jobs:"
  printf '      %s\n' "$retired"
  echo "    (replaced by the EU multi-market pilot scrapers — the scripts"
  echo "     themselves stay in the repo and can still be run by hand)"
fi

echo "==> Installing crontab (replacing any previous news-notifier entries)"
{
  crontab -l 2>/dev/null | grep -v "$APP_DIR/deploy/run.sh" || true
  sed "s#__APP_DIR__#$APP_DIR#g" deploy/crontab.txt
} | crontab -

echo "==> Installing log rotation"
sed "s#/home/\\*/news-notifier#$APP_DIR#g" deploy/logrotate.conf | sudo tee /etc/logrotate.d/news-notifier > /dev/null

cat <<EOF

Done.

Next steps:
  1. Edit $APP_DIR/.env — Discord webhook/user id, and the delivery
     destination the EU pilot quotes every market against.
  2. Check what's scheduled and what has run:  $APP_DIR/deploy/status.sh
  3. Tail a log after the next tick, e.g.:
       tail -f $APP_DIR/logs/delivery-multi.log
  4. Confirm cron itself is firing: grep CRON /var/log/syslog | tail

Beyblade tracking is now the EU multi-market pilot (se/de/fr/es), not
the old .se-only scripts. Those are unscheduled but still present, and
still runnable by hand if you need them.
EOF
