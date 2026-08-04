#!/usr/bin/env bash
# What's scheduled, when each job last ran, and how it went.
#
# Exists because "did the migration actually take effect?" is otherwise
# three commands and some squinting at timestamps — and because a cron
# job that silently stopped firing looks exactly like a quiet one.
#
# Usage: deploy/status.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "=== Scheduled jobs ==="
if crontab -l 2>/dev/null | grep -q "deploy/run.sh"; then
  crontab -l 2>/dev/null | grep "deploy/run.sh" | sed 's/^/  /'
else
  echo "  (none installed — run deploy/setup.sh)"
fi

echo
echo "=== Retired jobs still scheduled? ==="
stale="$(crontab -l 2>/dev/null \
  | grep -E "scripts/(track_delivery_date_playwright|scrape_hasbro_catalog)\.py" || true)"
if [ -n "$stale" ]; then
  echo "  WARNING — the superseded .se-only Beyblade jobs are still scheduled"
  echo "  alongside the EU pilot. Both track the same products, so you'll get"
  echo "  duplicate Discord alerts. Re-run deploy/setup.sh to fix."
  printf '    %s\n' "$stale"
else
  echo "  No. Good."
fi

echo
echo "=== Last run per log ==="
shopt -s nullglob
for log in logs/*.log; do
  last_start="$(grep -a "=== .* START " "$log" | tail -1)"
  last_end="$(grep -a "=== .* END \|=== .* SKIP " "$log" | tail -1)"
  printf '  %-24s %s\n' "$(basename "$log")" "${last_start:-(never started)}"
  printf '  %-24s %s\n' "" "${last_end:-(no completion recorded)}"
done
if [ -z "$(echo logs/*.log)" ]; then
  echo "  (no logs yet — nothing has run)"
fi

echo
echo "=== Recent failures (non-zero exits, last 20) ==="
failures="$(grep -ah "END .* (exit [^0]" logs/*.log 2>/dev/null | tail -20 || true)"
if [ -n "$failures" ]; then
  printf '  %s\n' "$failures"
else
  echo "  None."
fi

echo
echo "=== EU pilot state ==="
for dir in pilot/eu_multimarket/state/*/; do
  [ -d "$dir" ] || continue
  market="$(basename "$dir")"
  products="$( { grep -cvE '^\s*(#|$)' "$dir/products.txt" 2>/dev/null || echo 0; } | tr -d ' ')"
  tracked="$( { python3 -c "import json,sys;print(len(json.load(open('$dir/delivery_state.json'))))" 2>/dev/null || echo 0; } )"
  # find, not ls+glob: nullglob is on, so an unmatched glob disappears
  # entirely and `ls` would silently list the current directory instead.
  debug="$(find "$dir" -maxdepth 1 -name 'debug_*.html' 2>/dev/null | wc -l | tr -d ' ')"
  printf '  %-4s %3s catalogued   %3s delivery-tracked   %s debug dump(s)\n' \
    "$market" "$products" "$tracked" "$debug"
done
whitelist="pilot/eu_multimarket/state/whitelist.txt"
if [ -f "$whitelist" ]; then
  echo "  whitelist: $(grep -cvE '^\s*(#|$)' "$whitelist" | tr -d ' ') ASIN(s) delivery-tracked"
else
  echo "  WARNING — $whitelist is missing; the delivery tracker has nothing to check."
fi
