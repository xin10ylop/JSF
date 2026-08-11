#!/usr/bin/env bash
# Restart every coin bot, and PROVE it happened.
#
# `systemctl restart "jsf-paperbot@*"` matches zero units and exits 0. It
# looks like it worked, the && chain continues, and the bots keep running
# the old code -- observed live: uptimes of 41,000s straight after a
# "successful" restart, which then made a new health field look broken when
# it was simply absent. Never trust a glob to a template unit.
set -euo pipefail
cd "$(dirname "$0")/.."

COINS=$(python3 -c "import json;print(' '.join(json.load(open('bot/config.json')).get('coins',['btc'])))")
UNITS=$(for c in $COINS; do printf 'jsf-paperbot@%s ' "$c"; done)
echo "restarting: $UNITS"

before=$(for u in $UNITS; do
  systemctl show -p ExecMainStartTimestampMonotonic --value "$u" 2>/dev/null
done | tr '\n' ' ')

# shellcheck disable=SC2086
systemctl restart $UNITS
sleep 3

fail=0
for u in $UNITS; do
  up=$(systemctl show -p ExecMainStartTimestampMonotonic --value "$u" 2>/dev/null)
  act=$(systemctl is-active "$u" 2>/dev/null || echo inactive)
  age=$(( ( $(cut -d' ' -f1 /proc/uptime | cut -d. -f1) * 1000000 - ${up:-0} ) / 1000000 ))
  if [ "$act" != "active" ]; then
    echo "  $u: $act  !! NOT RUNNING"; fail=1
  elif [ "$age" -gt 60 ]; then
    echo "  $u: active but up ${age}s  !! DID NOT RESTART"; fail=1
  else
    echo "  $u: active, up ${age}s  ok"
  fi
done
echo "(was: $before)"
[ "$fail" = 0 ] || { echo "RESTART INCOMPLETE" >&2; exit 5; }
echo "all bots restarted"
