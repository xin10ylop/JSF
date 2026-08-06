#!/bin/bash
# One-shot setup for a fresh Ubuntu droplet. Run as root.
# Installs deps, clones the repo, and installs systemd services for the
# recorder and the paper bot. Idempotent.
set -e

REPO_URL="${REPO_URL:-https://github.com/xin10ylop/jsf.git}"
BRANCH="${BRANCH:-claude/polymarket-btc-binaries-cy1f9p}"
DIR=/opt/jsf

apt-get update -y
apt-get install -y python3 python3-pip git

if [ ! -d $DIR ]; then
  git clone --branch "$BRANCH" "$REPO_URL" $DIR
else
  cd $DIR && git pull
fi
cd $DIR
pip3 install -r requirements.txt --break-system-packages 2>/dev/null \
  || pip3 install -r requirements.txt

mkdir -p logs data/live

if [ ! -f .env ]; then
  echo "TELONEX_API_KEY=CHANGE_ME" > .env
  echo ">>> edit /opt/jsf/.env and set TELONEX_API_KEY <<<"
fi

cat > /etc/systemd/system/jsf-recorder.service <<'UNIT'
[Unit]
Description=JSF live data recorder (Binance/RTDS/CLOB)
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
Description=JSF paper trading bot (vacuum ladder)
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

systemctl daemon-reload
systemctl enable --now jsf-recorder jsf-paperbot
systemctl status jsf-recorder --no-pager -l | head -5
systemctl status jsf-paperbot --no-pager -l | head -5
echo "DONE. Logs: journalctl -u jsf-paperbot -f ; files in /opt/jsf/logs/"
