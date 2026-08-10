#!/bin/bash
cd /home/user/JSF
for C in btc eth sol xrp doge; do
  python3 src/fetch_pm_free.py trades --coins $C --start 2026-08-01 --end 2026-08-09 \
    --workers 14 --out data/pmfree_aug >> logs/aug_trades.log 2>&1
  echo "=== $C DONE ===" >> logs/aug_trades.log
done
echo ALLDONE >> logs/aug_trades.log
