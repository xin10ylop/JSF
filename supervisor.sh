#!/bin/bash
# Idempotent pipeline supervisor. Safe to re-run after any crash/restart:
# checks what exists and does the next needed step. Logs to logs/supervisor.log
cd /home/user/JSF
mkdir -p logs
L=logs/supervisor.log
log(){ echo "$(date -u +%H:%M:%S) $*" >> $L; }

# 0. recorder
if ! pgrep -f "bot/recorder.py" > /dev/null; then
  nohup python3 bot/recorder.py >> logs/recorder.log 2>&1 &
  log "recorder restarted"
fi

need_dl(){ # manifest raw_dir consolidated_name -> 0 if download needed
  local man=$1 raw=$2 cons=$3
  [ -f "data/consolidated/$cons.parquet" ] && return 1
  return 0
}

step(){ # family manifest
  local fam=$1 man=$2
  local raw="data/telonex/raw/$fam"
  if [ ! -f "data/consolidated/$fam.parquet" ]; then
    # download any missing files (downloader skips existing)
    log "downloading $fam"
    python3 src/download_telonex.py --manifest "$man" --out "$raw" --concurrency 10 >> logs/dl.log 2>&1
    log "consolidating $fam"
    python3 src/consolidate_trades.py "$fam" --purge >> logs/dl.log 2>&1
    log "$fam done"
  fi
}

step updown_15m_trades data/manifests/updown_15m_trades.csv
step updown_4h_trades data/manifests/updown_4h_trades.csv
step hourly_updown_trades data/manifests/hourly_updown_trades.csv
step updown_5m_trades data/manifests/updown_5m_trades.csv
step above_hourly_trades data/manifests/above_hourly_trades.csv

# book sample (no consolidation marker; use a stamp file)
if [ ! -f data/telonex/raw/book5_sample/.done ]; then
  log "downloading book5 sample"
  python3 src/download_telonex.py --manifest data/manifests/book5_sample.csv --out data/telonex/raw/book5_sample --concurrency 10 >> logs/dl.log 2>&1 \
    && touch data/telonex/raw/book5_sample/.done && log "book5 done"
fi

# grids
if [ -f data/consolidated/updown_15m_trades.parquet ] && [ ! -f data/grid_15m.parquet ]; then
  log "grid 15m"; python3 src/build_grid.py updown_15m_trades >> logs/analysis_15m.log 2>&1
fi
if [ -f data/consolidated/updown_5m_trades.parquet ] && [ ! -f data/grid_5m.parquet ]; then
  log "grid 5m"; python3 src/build_grid.py updown_5m_trades >> logs/analysis_5m.log 2>&1
fi
if [ -f data/consolidated/updown_4h_trades.parquet ] && [ ! -f data/grid_4h.parquet ]; then
  log "grid 4h"; python3 src/build_grid.py updown_4h_trades >> logs/analysis_4h.log 2>&1
fi
if [ -f data/telonex/raw/book5_sample/.done ] && [ ! -f data/book_grid_15m.parquet ]; then
  log "book grids"; python3 src/build_book_grid.py >> logs/analysis_15m.log 2>&1
fi

# analyses (stamped)
run_once(){ # stamp cmd...
  local stamp=$1; shift
  if [ ! -f "logs/.$stamp" ]; then
    log "run $stamp"
    "$@" >> "logs/${stamp}.log" 2>&1 && touch "logs/.$stamp" && log "$stamp ok" || log "$stamp FAILED"
  fi
}
if [ -f data/grid_15m.parquet ]; then
  run_once flb_15m python3 src/analysis_flb.py updown_15m_trades
  run_once paper_15m python3 src/analysis_paper.py 15m
  run_once edge_15m python3 src/analysis_edge.py 15m
  run_once prints_15m python3 src/analysis_prints.py updown_15m_trades
fi
if [ -f data/grid_5m.parquet ]; then
  run_once flb_5m python3 src/analysis_flb.py updown_5m_trades
  run_once paper_5m python3 src/analysis_paper.py 5m
  run_once edge_5m python3 src/analysis_edge.py 5m
  run_once prints_5m python3 src/analysis_prints.py updown_5m_trades
fi
if [ -f data/consolidated/hourly_updown_trades.parquet ]; then
  run_once flb_hourly python3 src/analysis_flb.py hourly_updown_trades
  run_once prints_hourly python3 src/analysis_prints.py hourly_updown_trades
fi
if [ -f data/consolidated/above_hourly_trades.parquet ]; then
  run_once flb_above python3 src/analysis_flb.py above_hourly_trades
  run_once prints_above python3 src/analysis_prints.py above_hourly_trades
fi
if [ -f data/consolidated/updown_4h_trades.parquet ] && [ -f data/grid_4h.parquet ]; then
  run_once flb_4h python3 src/analysis_flb.py updown_4h_trades
  run_once paper_4h python3 src/analysis_paper.py 4h
  run_once edge_4h python3 src/analysis_edge.py 4h
fi
log "supervisor pass complete"
echo SUPERVISOR-PASS-COMPLETE
