#!/bin/bash
# Install the shadow/live executor units. Run as root on the droplet,
# AFTER deploy/droplet_setup.sh (paper fleet + recorder) and AFTER
# /opt/jsf/.env holds POLYMARKET_PRIVATE_KEY (+ POLYMARKET_FUNDER).
# Idempotent.
#
# The live bots get their OWN slice, not jsf.slice: the paper fleet
# already runs near that slice's ceiling, and cgroup reclaim stalling
# the MONEY path because a paper bot leaked is the wrong failure
# coupling in both directions. Three bots measured ~50 MB RSS each.
set -e

DIR=/opt/jsf
cd $DIR

if [ ! -f .env ] || ! grep -q '^POLYMARKET_PRIVATE_KEY=' .env; then
  echo "FATAL: $DIR/.env with POLYMARKET_PRIVATE_KEY is required first"
  echo "       (shadow mode runs without it, but installing units that"
  echo "        will flip to live without the key ready is how a mode"
  echo "        flip turns into a silent outage)"
  exit 1
fi
chmod 600 .env

if ! python3 -c "import polymarket" 2>/dev/null; then
  # typing-extensions first and --ignore-installed: pip cannot uninstall
  # the debian-owned copy and aborts the whole install otherwise.
  pip3 install --break-system-packages --ignore-installed typing-extensions
  pip3 install --break-system-packages 'polymarket-client>=0.6'
fi
python3 -c "import dotenv" 2>/dev/null \
  || pip3 install --break-system-packages python-dotenv

cat > /etc/systemd/system/jsf-live.slice <<'UNIT'
[Unit]
Description=JSF shadow/live executors, memory-isolated from the paper fleet

[Slice]
MemoryHigh=250M
MemoryMax=350M
UNIT

cat > /etc/systemd/system/jsf-livebot@.service <<'UNIT'
[Unit]
Description=JSF shadow/live bot for %i (mode set in bot/config.live.json)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/jsf
ExecStart=/usr/bin/python3 bot/run.py --coin %i --cfg bot/config.live.json
Slice=jsf-live.slice
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload

COINS=$(python3 -c "import json;print(' '.join(json.load(open('bot/config.live.json')).get('coins',['btc'])))")
UNITS=$(for c in $COINS; do printf 'jsf-livebot@%s ' "$c"; done)
echo "live units: $UNITS"
# shellcheck disable=SC2086
systemctl enable --now $UNITS
sleep 3
for u in $UNITS; do systemctl --no-pager --plain status "$u" | head -3; done

MODE=$(python3 -c "import json;print(json.load(open('bot/config.live.json')).get('mode'))")
cat <<MSG

DONE. mode: $MODE
  order log        tail -f /opt/jsf/logs/live/btc/orders.jsonl
  live decisions   tail -f /opt/jsf/logs/live/btc/decisions.jsonl
  divergence       cd /opt/jsf && python3 src/live_vs_paper.py
  flip to live     edit mode in bot/config.live.json (via a pushed commit),
                   git pull, systemctl restart $UNITS
MSG
