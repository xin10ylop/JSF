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
# chrony separately and non-fatally: this script runs under set -e, and a
# clock-daemon hiccup must never abort the deploy half-way -- that leaves
# OLD unit files installed while the repo advances, the exact silent
# partial-deploy failure this script exists to prevent.
apt-get install -y chrony || echo "WARN: chrony install failed; timesyncd stays"

# --- clock: the strategy's gate lives in the last 30s and rem enters z as
# rem^3, so a couple of seconds of skew systematically misprices every
# market. chrony steps the clock hard on boot and keeps it within ms after;
# timesyncd (the Ubuntu default) is fine most days but can drift for hours
# after a suspend/migration without complaining.
systemctl enable --now chrony 2>/dev/null || \
  systemctl enable --now chronyd 2>/dev/null || \
  echo "WARN: no chrony service; check timedatectl"
sleep 2
chronyc tracking 2>/dev/null | head -5 || true

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
# Grow total swap to >= 2G by ADDING a file, never by replacing one.
# `swapoff` on a box whose swap is in use forces every swapped-out page
# back into RAM at once, and this droplet hosts processes that are not
# ours (/opt/polymarketstrat) -- taking swap away could OOM-kill them.
# Adding a second file is additive and safe at any moment.
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
SWAP_MB=$(free -m | awk '/^Swap:/{print $2}')
if [ "${SWAP_MB:-0}" -lt 2000 ] && [ ! -f /swapfile2 ]; then
  echo "swap is ${SWAP_MB}MB; adding /swapfile2 rather than replacing /swapfile"
  fallocate -l 2G /swapfile2 || dd if=/dev/zero of=/swapfile2 bs=1M count=2048
  chmod 600 /swapfile2
  mkswap /swapfile2
  swapon /swapfile2
  grep -q '^/swapfile2' /etc/fstab || echo '/swapfile2 none swap sw 0 0' >> /etc/fstab
fi
echo 'vm.swappiness=20' > /etc/sysctl.d/99-jsf.conf
sysctl -p /etc/sysctl.d/99-jsf.conf >/dev/null
free -h

# Everything JSF runs inside one slice with a hard memory ceiling. This box
# may host other people's work (observed: /opt/polymarketstrat), and five
# bots plus a recorder measured ~905 MB with the daily edge check pushing
# the peak past 1.1 GB -- without a ceiling a leak here takes down a
# neighbour we were asked not to touch. MemoryHigh throttles and reclaims
# first; MemoryMax is the wall.
#
# On a 2 GB host this leaves ~500 MB for the OS and anything else running.
# Measure the neighbour before trusting that:
#   systemd-cgtop -b -n1 --order=memory | head
# and lower these if it needs more. Nothing in JSF is OOM-protected any
# more -- OOMScoreAdjust=-500 used to make the kernel prefer to kill
# something else, which is the wrong default on a shared box.
cat > /etc/systemd/system/jsf.slice <<'UNIT'
[Unit]
Description=JSF bots and recorder, memory-bounded as a group

[Slice]
MemoryHigh=700M
MemoryMax=900M
UNIT

cat > /etc/systemd/system/jsf-recorder.service <<'UNIT'
[Unit]
Description=JSF live data recorder (Binance / Chainlink RTDS / Polymarket CLOB)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/jsf
ExecStart=/usr/bin/python3 bot/recorder.py
Slice=jsf.slice
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
Slice=jsf.slice
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
Slice=jsf.slice
ExecStart=/usr/bin/python3 bot/prune.py --window 90 --keep-raw-hours 1
# Retention for the tick captures the pruner does not distil. The oracle
# capture (rtds) feeds tape_chainlink and the bots' restart backfill; a
# week is plenty for both, and unbounded it eventually eats the disk.
ExecStart=/usr/bin/find /opt/jsf/data/live/rtds -name '*.jsonl' -mtime +7 -delete
ExecStart=/usr/bin/find /opt/jsf/data/live/binance -name '*.jsonl' -mtime +7 -delete
UNIT

cat > /etc/systemd/system/jsf-prune.timer <<'UNIT'
[Unit]
Description=Run the JSF book pruner every 20 minutes

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
MemoryMax=600M
Nice=10
# NOT in jsf.slice: its 600M allowance on top of the bots pushed the
# slice past MemoryMax=900M at 02:30 UTC daily -- cgroup reclaim stalls
# every bot's event loop mid-settle-window, or the OOM killer takes one.
ExecStart=/usr/bin/python3 src/daily_edge_check.py
# The self-sufficient decay row: daily_edge_check's kline zips end at
# 2026-08-09 and it silently stopped appending. The hybrid tape fetches
# everything itself and measures the edge on the CORRECT input series.
ExecStart=/usr/bin/python3 -u src/tape_chainlink.py --hours 24 --picks first --csv reports/hybrid_decay.csv
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
systemctl start jsf.slice 2>/dev/null || true
# shellcheck disable=SC2086
systemctl enable --now jsf-recorder $BOT_UNITS jsf-prune.timer jsf-edgecheck.timer
sleep 3
systemctl --no-pager --plain status jsf-recorder | head -4
for u in $BOT_UNITS; do systemctl --no-pager --plain status "$u" | head -3; done
free -m | head -2
echo "jsf.slice ceiling: $(cat /sys/fs/cgroup/jsf.slice/memory.max 2>/dev/null || echo unknown)"
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
