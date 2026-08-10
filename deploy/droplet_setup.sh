#!/bin/bash
# One-shot setup for a fresh Ubuntu droplet. Run as root. Idempotent.
#
# Installs four units:
#   jsf-recorder    live Binance / Chainlink-RTDS / CLOB capture
#   jsf-paperbot    paper trading on the verified trailing-TWAP contract
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
  cd $DIR && git fetch origin "$BRANCH" && git checkout "$BRANCH" && git pull
fi
cd $DIR
pip3 install -r requirements.txt --break-system-packages 2>/dev/null \
  || pip3 install -r requirements.txt

mkdir -p logs data/live data/live/books reports

cat > /etc/systemd/system/jsf-recorder.service <<'UNIT'
[Unit]
Description=JSF live data recorder (Binance / Chainlink RTDS / Polymarket CLOB)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/jsf
ExecStart=/usr/bin/python3 bot/recorder.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/jsf-paperbot.service <<'UNIT'
[Unit]
Description=JSF paper bot (endgame taker, post-2026-08-07 trailing-TWAP contract)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/jsf
ExecStart=/usr/bin/python3 bot/run.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

# --- disk guard: the recorder writes ~6.5 GB/day of raw book JSONL ---------
cat > /etc/systemd/system/jsf-prune.service <<'UNIT'
[Unit]
Description=JSF book pruner (distil endgame snapshots, reclaim disk)

[Service]
Type=oneshot
WorkingDirectory=/opt/jsf
ExecStart=/usr/bin/python3 bot/prune.py --window 90 --keep-raw-hours 2
UNIT

cat > /etc/systemd/system/jsf-prune.timer <<'UNIT'
[Unit]
Description=Run the JSF book pruner hourly

[Timer]
OnCalendar=hourly
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
systemctl enable --now jsf-recorder jsf-paperbot jsf-prune.timer jsf-edgecheck.timer
sleep 3
systemctl --no-pager --plain status jsf-recorder | head -4
systemctl --no-pager --plain status jsf-paperbot | head -4
systemctl --no-pager list-timers 'jsf-*' || true
cat <<'MSG'

DONE.
  live bot log     journalctl -u jsf-paperbot -f
  pricer health    grep '"health"' /opt/jsf/logs/decisions.jsonl | tail -1
  paper fills      tail -f /opt/jsf/logs/paper_fills.jsonl
  decay history    column -s, -t /opt/jsf/reports/decay_log.csv
  ex-ante depth    cd /opt/jsf && PYTHONPATH=src python3 src/depth_sim.py
MSG
