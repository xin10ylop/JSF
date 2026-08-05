# Results Table (running)

All confidence intervals are cluster-bootstrap by market unless noted.
"Tape grid" = tape-implied quotes (last aggressive print per side, fresh<10s,
two-sided); "Book grid" = real book_snapshot_5 sample (~1000 mkts/family).

| # | Hypothesis / test | Data | Result | Status |
|---|---|---|---|---|
| S1 | Settlement convention updown = first Chainlink round >= boundary | 47,590 clean mkts | 99.9958% match (2 miss) | VERIFIED |
| S2 | Settlement hourly = Binance 1H candle O/C | 7,385 mkts | 100% match | VERIFIED |
| S3 | Settlement above = Binance 1H close > strike, title hour = candle END | 46,320 mkts | 100% match | VERIFIED |
| S4 | Oracle broadcast lag | 10.56M ticks | median 1.12s, p99 2.02s | MEASURED |
| S5 | Binance leads oracle round | 2d @1s | corr 0.71 gap vs next-3s oracle move, slope 0.82; |gap|>2bps (2.4% of s) -> ~3bps capture | MEASURED |
| S6 | Chainlink-Binance basis | 1w | -8bps median, std 1.9bps | MEASURED |
| S7 | Realized oracle window var / trailing BN RV forecast | 12K windows | 1.25 (trimmed) - 1.5 (raw) | MEASURED |
| H4a | FLB favorite-space, 15m tape grid, in-window fresh | 7,022 mkts Apr-Aug26 | fav buckets: 0.5-0.6 +0.81pp [0.10,1.45]; 0.6-0.7 +1.12 [0.15,2.06]; 0.8-0.9 +1.88 [0.98,2.81]; 0.9-0.97 +1.06 [0.44,1.74]; 0.97-1 +0.41 [0.13,0.71]; holds on BOTH sides separately | POSITIVE (train-era data; to re-verify on full sample + test split) |
| H4b | FLB favorite-space, 15m book grid | 974 mkts | same sign in 0.7-0.8/0.8-0.9/0.97-1 but CIs span 0 (small n); side-split shows BTC downtrend contamination in Up-space | INCONCLUSIVE (n too small) |
| H4c | FLB favorite-space, 5m book grid | 995 mkts | 0.97-1.0: +1.02pp [0.08,1.62]; others mixed | WEAK POSITIVE |
| H11 | Crossed books | 250 mkts book sample | median cross -1c, median crossable 24 sh, in 124/250 mkts | SCRAPS - not a strategy; likely reporting artifact |
| U1 | Up-token mid-level bias in raw Up-space calibration | book grid | all-bucket negative gaps = BTC downtrend, NOT venue bias | CONFOUND IDENTIFIED - use favorite space |
