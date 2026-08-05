# Recovery / rebuild instructions

The container can be recycled at any time; `data/` is never committed. Full
rebuild from a fresh clone:

```bash
pip install telonex pandas numpy scipy pyarrow duckdb matplotlib statsmodels scikit-learn requests python-dotenv
echo 'TELONEX_API_KEY=<key>' > .env

# 1. Binance 1s klines + aggTrades (public)   [~10 min]
bash scripts/dl_binance.sh          # or see session notes; writes data/binance/
python3 -c "see src/build_masters.py header"  # zips -> data/binance/parquet/

# 2. Chainlink settlement feed                 [~1 min]
python3 src/download_telonex.py --manifest data/manifests/crypto_prices.csv \
    --out data/telonex/crypto_prices --concurrency 6
# consolidate to data/telonex/chainlink_btcusd.parquet (see build_masters.py)

# 3. Masters + manifests                       [~5 min]
python3 src/build_masters.py

# 4. Vol grid                                  [~3 min]
python3 src/vol.py

# 5. Trade tapes + book sample                 [~3 h, ~145K API downloads]
#    (order: 15m, 4h, hourly, 5m, above, book5_sample; consolidate+purge each)
python3 src/download_telonex.py --manifest data/manifests/updown_15m_trades.csv \
    --out data/telonex/raw/updown_15m_trades --concurrency 10
python3 src/consolidate_trades.py updown_15m_trades --purge
# ... repeat per family ...

# 6. Decision grids
python3 src/build_grid.py updown_15m_trades updown_5m_trades updown_4h_trades
python3 src/build_book_grid.py
```

Key verified facts are in reports/venue_facts.md; running results in
reports/results_table.md. Commit and push after every milestone.
