#!/bin/bash
# One-shot setup for a fresh Ubuntu droplet. Run as root. Idempotent.
#
# Installs four units:
#   jsf-recorder    live Binance / Chainlink-RTDS / CLOB capture
#   jsf-paperbot@X  paper trading on the verified trailing-TWAP contract,
#                   one instance per coin in config.json's `coins`
#   jsf-prune       hourly: distil endgame books, delete raw (disk guard)
#   jsf-edgecheck   daily: frozen-parameter decay monitor
#
# No API key is required. The whole data pipeline runs on public,
# unauthenticated Polymarket and Binance endpoints.
set -e

REPO_URL="${REPO_URL:-https://github.com/xin10ylop/jsf.git}"
BRANCH="${BRANCH:-claude/polymarket-btc-binaries-cy1f9p}"
DIR=/opt/jsf

apt-get update -y
apt-get install -y python3 python3-pip git

if [ ! -d $DIR ]; then
  git clone --branch "$BRANCH" "$REPO_URL" $DIR
else
  # Hard-reset to origin. A tracked file edited on the host (probe_latency
  # used to write rtt_ms straight into bot/config.json) makes `git pull`
  # abort, and the rest of this script then happily "succeeds" against the
  # OLD code -- which is how the multi-coin build appeared to deploy and
  # did not. Host-specific settings live in bot/config.local.json, which
  # is gitignored and survives this.
  cd $DIR && git fetch origin "$BRANCH" \
    && git checkout -B "$BRANCH" "origin/$BRANCH" \
    && git reset --hard "origin/$BRANCH"
fi
echo "deployed commit: $(cd $DIR && git rev-parse --short HEAD)"
cd $DIR
pip3 install -r requirements.txt --break-system-packages 2>/dev/null \
  || pip3 install -r requirements.txt

mkdir -p logs data/live data/live/books reports

# --- swap: a 512MB droplet cannot hold two pandas/scipy processes ----------
# The recorder and the bot each carry ~150-250 MB RSS once numpy/scipy are
# imported, and the daily edge check loads parquet on top of that. Without
# swap the box OOMs and drops SSH.
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo 'vm.swappiness=20' > /etc/sysctl.d/99-jsf.conf && sysctl -p /etc/sysctl.d/99-jsf.conf
fi
free -h

cat > /etc/systemd/system/jsf-recorder.service <<'UNIT'
[Unit]
Description=JSF live data recorder (Binance / Chainlink RTDS / Polymarket CLOB)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/jsf
ExecStart=/usr/bin/python3 bot/recorder.py
OOMScoreAdjust=-500
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

# One bot process per coin: BotState carries a single oracle buffer and vol
# estimator, so the coin is a property of the process. ~46 MB RSS each.
cat > /etc/systemd/system/jsf-paperbot@.service <<'UNIT'
[Unit]
Description=JSF paper bot for %i (endgame taker, trailing-TWAP contract)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/jsf
ExecStart=/usr/bin/python3 bot/run.py --coin %i
OOMScoreAdjust=-500
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

# The pre-multi-coin single unit, if present, would run a second btc bot
# against the same logs/btc/bot.lock and flap on the lock forever.
if systemctl list-unit-files | grep -q '^jsf-paperbot\.service'; then
  systemctl disable --now jsf-paperbot.service 2>/dev/null || true
  rm -f /etc/systemd/system/jsf-paperbot.service
fi

COINS=$(python3 -c "import json;print(' '.join(json.load(open('/opt/jsf/bot/config.json')).get('coins',['btc'])))")
BOT_UNITS=$(for c in $COINS; do printf 'jsf-paperbot@%s ' "$c"; done)
echo "bot units: $BOT_UNITS"

# --- disk guard: the recorder writes ~6.5 GB/day of raw book JSONL ---------
cat > /etc/systemd/system/jsf-prune.service <<'UNIT'
[Unit]
Description=JSF book pruner (distil endgame snapshots, reclaim disk)

[Service]
Type=oneshot
WorkingDirectory=/opt/jsf
ExecStart=/usr/bin/python3 bot/prune.py --window 90 --keep-raw-hours 1
UNIT

cat > /etc/systemd/system/jsf-prune.timer <<'UNIT'
[Unit]
Description=Run the JSF book pruner hourly

[Timer]
OnCalendar=*:00/20
Persistent=true

[Install]
WantedBy=timers.target
UNIT

# --- decay monitor: frozen parameters, appends reports/decay_log.csv -------
cat > /etc/systemd/system/jsf-edgecheck.service <<'UNIT'
[Unit]
Description=JSF daily edge / decay check

[Service]
Type=oneshot
WorkingDirectory=/opt/jsf
MemoryMax=700M
Nice=10
ExecStart=/usr/bin/python3 src/daily_edge_check.py
UNIT

cat > /etc/systemd/system/jsf-edgecheck.timer <<'UNIT'
[Unit]
Description=Run the JSF edge check daily

[Timer]
OnCalendar=*-*-* 02:30:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
# shellcheck disable=SC2086
systemctl enable --now jsf-recorder $BOT_UNITS jsf-prune.timer jsf-edgecheck.timer
sleep 3
systemctl --no-pager --plain status jsf-recorder | head -4
for u in $BOT_UNITS; do systemctl --no-pager --plain status "$u" | head -3; done
free -m | head -2
systemctl --no-pager list-timers 'jsf-*' || true
cat <<'MSG'

DONE.
  all coins        cd /opt/jsf && python3 src/status.py
  live bot log     journalctl -u jsf-paperbot@eth -f
  paper fills      tail -f /opt/jsf/logs/eth/paper_fills.jsonl
  one coin scored  cd /opt/jsf && python3 src/score_paper.py --coin eth
  decay history    column -s, -t /opt/jsf/reports/decay_log.csv
  ex-ante depth    cd /opt/jsf && PYTHONPATH=src python3 src/depth_sim.py
MSG
