#!/usr/bin/env bash
# Archive the paper record and start a clean one.
#
# Every fill before 0ca2161 was produced by a broker that reached its price
# 96% of the time and took 100% of the displayed size, when recorded books
# say the real figures are ~65% and ~50%. Those fills are not a baseline to
# compare against -- they are a different experiment. Mixing them into the
# same log means the scorer averages two incompatible fill models for
# hours, which is exactly what happened with the zero-latency fills before.
#
# Nothing is deleted; the old logs move to logs/archive/<stamp>/ and stay
# readable with:  python3 src/score_paper.py --log logs/archive/<stamp>/paper_fills.jsonl
#
# Usage (on the trading host):  bash deploy/reset_pnl.sh
set -euo pipefail
cd "$(dirname "$0")/.."

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
DEST="logs/archive/$STAMP"
mkdir -p "$DEST"

# Stop first: the bot holds these files open in append mode, and rotating
# them underneath a running process leaves fills written to a deleted inode.
COINS=$(python3 -c "import json;print(' '.join(json.load(open('bot/config.json')).get('coins',['btc'])))")
UNITS=$(for c in $COINS; do printf 'jsf-paperbot@%s ' "$c"; done)
for u in $UNITS jsf-paperbot; do
  systemctl is-active --quiet "$u" 2>/dev/null && systemctl stop "$u" \
    && echo "stopped $u"
done

# logs/<coin>/*.jsonl for the multi-coin build, plus the flat legacy pair
for src in $(ls logs/*/paper_fills.jsonl logs/*/decisions.jsonl \
             logs/paper_fills.jsonl logs/decisions.jsonl 2>/dev/null); do
  [ -s "$src" ] || continue
  mkdir -p "$DEST/$(dirname "${src#logs/}")"
  mv "$src" "$DEST/${src#logs/}"
  echo "archived $src -> $DEST/${src#logs/}  ($(wc -l < "$DEST/${src#logs/}") lines)"
  : > "$src"     # recreate empty; the flock file is separate, leave it alone
done

for u in $UNITS; do
  systemctl start "$u" && echo "started $u"
done

echo
echo "clean record started at $STAMP"
echo "old record: python3 src/score_paper.py --log $DEST/<coin>/paper_fills.jsonl"
