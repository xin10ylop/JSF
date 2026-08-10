#!/bin/bash
cd /home/user/JSF
for C in eth sol xrp doge btc; do
  python3 src/fetch_pm_free.py trades --coins $C --start 2026-03-01 --end 2026-08-09 \
    --workers 24 --out data/pmfree_x4 --only-slugs data/slugs_x4.parquet \
    >> logs/x4_trades.log 2>&1
  echo "=== $C COMPLETE ===" >> logs/x4_trades.log
done
echo "ALL COMPLETE" >> logs/x4_trades.log
