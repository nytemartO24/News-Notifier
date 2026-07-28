#!/usr/bin/env bash
# Cron entry point: runs one of the scraper scripts once and exits.
# Usage: run.sh scripts/track_delivery_date_playwright.py
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

set -a
source .env
set +a

# RUN_ONCE tells the delivery tracker to do a single check-and-exit cycle
# instead of its internal persistent loop — the crontab entries provide the
# interval instead. The other two scripts always run once regardless.
export RUN_ONCE=true

# Bracket every invocation with markers so the log shows a run happened
# even when the script itself prints nothing (e.g. scrape_hypixel.py stays
# silent when there's nothing new) — otherwise a quiet log is
# indistinguishable from cron never firing at all.
echo "=== $(date -Is) START $1 ==="
set +e
"$REPO_ROOT/.venv/bin/python" "$1"
status=$?
set -e
echo "=== $(date -Is) END $1 (exit $status) ==="
exit "$status"
