#!/usr/bin/env bash
# Cron entry point: runs one scraper once and exits.
#
# Usage: run.sh <script-path> [args...]
#   run.sh scripts/scrape_hypixel.py
#   run.sh pilot/eu_multimarket/track_delivery_multi.py --send-discord
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

set -a
# shellcheck disable=SC1091
source .env
set +a

# RUN_ONCE tells the legacy delivery tracker to do a single
# check-and-exit cycle instead of its internal persistent loop — the
# crontab entries provide the interval instead. Every other script runs
# once regardless and ignores it.
export RUN_ONCE=true

script="$1"
shift

# Don't let a slow run pile up behind itself. The pilot scrapers drive a
# real browser across four marketplaces, so a bad network day can push a
# run past its next scheduled tick; without this you'd get two Chromium
# instances competing and — worse — two processes writing the same state
# file. Keyed per script, so the delivery and catalog jobs can still
# overlap each other freely; they touch different state.
lock_file="/tmp/news-notifier-$(printf '%s' "$script" | tr '/.' '--').lock"
exec 9>"$lock_file"
if ! flock -n 9; then
  echo "=== $(date -Is) SKIP $script (previous run still in progress) ==="
  exit 0
fi

# Bracket every invocation with markers so the log shows a run happened
# even when the script itself prints nothing (e.g. scrape_hypixel.py stays
# silent when there's nothing new) — otherwise a quiet log is
# indistinguishable from cron never firing at all.
echo "=== $(date -Is) START $script $* ==="
set +e
"$REPO_ROOT/.venv/bin/python" "$script" "$@"
status=$?
set -e
echo "=== $(date -Is) END $script (exit $status) ==="
exit "$status"
