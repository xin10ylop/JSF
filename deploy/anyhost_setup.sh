#!/bin/bash
# Portable LIVE-ONLY setup. Runs the money path on any Ubuntu/Debian box:
# a $4 VPS, a free-tier micro instance, a spare laptop, a Raspberry Pi.
#
# Why this exists separately from droplet_setup.sh: that script installs
# the whole research estate -- recorder (~6.5GB/day of disk), the paper
# fleet, prune timers, edgecheck. None of it is required to TRADE. The
# live bot opens its own Binance and Polymarket RTDS websockets and
# reads no recorded file, so the money path alone is ~50MB RSS and
# almost no disk. That fits anywhere.
#
# Usage (as root, or with sudo):
#   POLYMARKET_PRIVATE_KEY=0x... POLYMARKET_FUNDER=0x... \
#     bash deploy/anyhost_setup.sh btc
#
# Or put those two in /opt/jsf/.env first and just: bash anyhost_setup.sh
set -e

COIN="${1:-btc}"
REPO_URL="${REPO_URL:-https://github.com/xin10ylop/jsf.git}"
BRANCH="${BRANCH:-claude/polymarket-btc-binaries-cy1f9p}"
DIR="${DIR:-/opt/jsf}"

echo "==> installing prerequisites"
if command -v apt-get >/dev/null; then
  apt-get update -y
  apt-get install -y python3 python3-pip git
  # Clock accuracy is not cosmetic here: the gate lives in the last 30
  # seconds and rem enters the z-score as rem^3, so seconds of skew
  # misprice every market. Non-fatal -- a clock daemon hiccup must never
  # abort a deploy half-way.
  apt-get install -y chrony || echo "WARN: chrony missing; check timedatectl"
  systemctl enable --now chrony 2>/dev/null || \
    systemctl enable --now chronyd 2>/dev/null || true
fi

echo "==> fetching code"
if [ ! -d "$DIR" ]; then
  git clone --branch "$BRANCH" "$REPO_URL" "$DIR"
else
  git -C "$DIR" fetch origin "$BRANCH"
  git -C "$DIR" checkout "$BRANCH"
  git -C "$DIR" reset --hard "origin/$BRANCH"
fi
cd "$DIR"

echo "==> installing the LIVE dependency set only"
# numpy: bot/state.py. websockets: the two feeds. polymarket-client: the
# signing + order path. pandas/scipy/sklearn are research-only (prune,
# and a lazily-imported pricer the live gate never calls).
pip3 install --break-system-packages -q \
  numpy websockets 'polymarket-client>=0.6' python-dotenv 2>/dev/null || \
  pip3 install -q numpy websockets 'polymarket-client>=0.6' python-dotenv

echo "==> credentials"
if [ -n "$POLYMARKET_PRIVATE_KEY" ]; then
  touch .env && chmod 600 .env
  grep -q '^POLYMARKET_PRIVATE_KEY=' .env 2>/dev/null || \
    echo "POLYMARKET_PRIVATE_KEY=$POLYMARKET_PRIVATE_KEY" >> .env
  if [ -n "$POLYMARKET_FUNDER" ]; then
    grep -q '^POLYMARKET_FUNDER=' .env 2>/dev/null || \
      echo "POLYMARKET_FUNDER=$POLYMARKET_FUNDER" >> .env
  fi
fi
if [ ! -f .env ] || ! grep -q '^POLYMARKET_PRIVATE_KEY=' .env; then
  echo "FATAL: $DIR/.env needs POLYMARKET_PRIVATE_KEY (and POLYMARKET_FUNDER"
  echo "       for a proxy wallet). Never paste the key into a shell that"
  echo "       records history; write the file directly."
  exit 1
fi
chmod 600 .env
mkdir -p logs/live/"$COIN"

if command -v systemctl >/dev/null && [ -d /run/systemd/system ]; then
  echo "==> installing systemd unit (auto-restart, survives reboot)"
  cat > /etc/systemd/system/jsf-live@.service <<UNIT
[Unit]
Description=JSF live bot (%i)
After=network-online.target chrony.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$DIR
EnvironmentFile=$DIR/.env
ExecStart=/usr/bin/python3 -u -m bot.run --cfg bot/config.live.json --coin %i
Restart=always
RestartSec=5
StandardOutput=append:$DIR/logs/live/%i/stdout.log
StandardError=append:$DIR/logs/live/%i/stdout.log

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable --now "jsf-live@$COIN"
  sleep 20
  systemctl is-active "jsf-live@$COIN" || true
  echo "--- last lines ---"
  tail -5 "$DIR/logs/live/$COIN/stdout.log" 2>/dev/null || true
  echo
  echo "status:  systemctl status jsf-live@$COIN"
  echo "logs:    journalctl -u jsf-live@$COIN -f"
  echo "stop:    systemctl stop jsf-live@$COIN"
else
  # No systemd: laptop, WSL, macOS, container. nohup keeps it alive for
  # the session; it does NOT survive a reboot or a closed lid.
  echo "==> no systemd here; starting under nohup"
  set -a; . ./.env; set +a
  nohup python3 -u -m bot.run --cfg bot/config.live.json --coin "$COIN" \
    >> "logs/live/$COIN/stdout.log" 2>&1 &
  echo "pid $! -- tail -f $DIR/logs/live/$COIN/stdout.log"
  echo "stop:  pkill -f 'bot.run .*--coin $COIN'"
fi
