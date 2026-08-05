# Venue Facts: Polymarket Short-Dated BTC Binaries

Everything below was verified directly from primary sources on 2026-08-05.
Sources: official Polymarket docs (docs.polymarket.com), market metadata and
descriptions from the Telonex markets dataset (2.53M markets), Telonex data
files themselves, and press coverage where noted.

## 1. Market families (BTC, short-dated)

| Family (slug pattern) | Markets | With trades | Window | Resolution source | Rule |
|---|---|---|---|---|---|
| `btc-updown-5m-<ts>` | 61,943 | 49,308 (from 2026-02-12) | 5 min | Chainlink BTC/USD data stream | Up iff end >= start |
| `btc-updown-15m-<ts>` | 28,816 | 26,444 (from 2025-10-11) | 15 min | Chainlink BTC/USD data stream | Up iff end >= start |
| `btc-updown-4h-<ts>` | 1,735 | 1,697 | 4 h | Chainlink BTC/USD data stream | Up iff end >= start |
| `bitcoin-up-or-down-<mon>-<d>-<h>{am,pm}-et` | ~6,500 | ~3,530 | 1 h | Binance BTC/USDT 1H candle | Up iff close >= open |
| `bitcoin-above-<strike>-on-<mon>-<d>-<yr>-<h>{am,pm}-et` | 46,540 | ~37,400 | 1 h, strike | Binance BTC/USDT 1H candle close | Yes iff close > strike |
| `bitcoin-above-<K>k-on-<mon>-<d>` (daily) | 2,793 | 2,409 | daily | Binance 1m candle close 12:00 ET | Yes iff close > strike |
| reach/dip/range daily families | ~4,700 | ~4,300 | daily | Binance | various |

Notes:
- The slug timestamp on updown markets is the **window start** (unix seconds).
  `end_date_us` = window end. Markets are created ~24h before the window;
  nearly all activity happens inside the window itself (empirical: 794 of 814
  trades in-window for a sampled 15m market).
- Ties resolve **Up** ("greater than or equal").
- `result_id` gives the on-chain outcome for every resolved market;
  `settled_at_us` shows settlement ~20-60s after window end.

## 2. Settlement mechanics (verified empirically)

- **updown 5m/15m/4h**: resolved against Polymarket's broadcast of Chainlink
  BTC/USD data-stream ticks. Telonex `crypto_prices` records those exact
  ticks (from 2026-04-02), 1s cadence, with three timestamps:
  - `timestamp_us` - Chainlink price-round time,
  - `server_timestamp_us` - Polymarket broadcast (median +1.12s, p99 +2.0s),
  - `local_timestamp_us` - Telonex receipt (~+0.2s after broadcast).
  **Verified convention**: start price = first round with ts >= t0; end price
  = first round with ts >= t1; tie -> Up. Match rate 99.9958% on 47,590
  clean markets (2 unexplained); mismatches otherwise are collector gaps.
- **hourly / daily families**: resolved on finalized Binance BTC/USDT candles.
  Verified: hourly updown 100% (n=7,385), above-strike 100% (n=46,320) when
  computed from Binance 1s klines.
- **Lead-lag**: Chainlink round at t correlates most with Binance returns at
  t-2s (corr 0.63 at 1s grid); broadcast adds ~1.1s. Watching Binance gives
  a ~2.5-3.5s information lead on the settlement index. Basis: Chainlink sits
  ~8bps below Binance BTCUSDT (USDT premium), std ~2bps.
- Oracle feed gaps exist (36 gaps > 5min in 4 months; worst days 2026-06-11,
  06-05, 06-10, 06-01) - skip/flag markets whose windows touch a gap.

## 3. Fees (verified at docs.polymarket.com/polymarket-learn/trading/fees)

- **Taker fee**: `fee = C x feeRate x p x (1 - p)`, crypto feeRate = **0.07**
  -> max 1.75c/share at p = 0.50, ~0 at extremes.
- **Makers pay zero.** Maker rebates 15-25% of collected taker fees.
- Fees introduced ~2026-01-07 on 15-minute crypto markets; published schedule
  now covers category "Crypto" at 0.07. Backtests charge full 0.07 on every
  taker fill regardless of date; maker rebates not credited (conservative).
- No deposit/withdrawal fees; winning shares redeem $1.00.

## 4. Microstructure (measured)

- Tick $0.001; min order ~5 shares. Median in-window spread 1.0c; ~13k quote
  updates per 15m window; top-of-book ~110-480 shares (~$100-300/side in the
  belly). Depth thins late; last-20s top sizes are huge at the extremes.
- Books can cross briefly (venue reporting artifact; median cross -1c, median
  crossable size 24 shares - scraps, not a strategy).
- Trades `side` = aggressor. Up/Down tapes are mirrored; one tape per market
  contains everything (verified identical totals).

## 5. Capital mechanics

- Buying costs p x C upfront; capital tied until resolution (minutes).
- Shorting = buying the sibling outcome. USDC on Polygon; no per-trade gas
  for CLOB trades.

## 6. Implications

1. Taker at p=0.5 pays ~1.75c fee + ~0.5c half-spread = ~2.25c/share all-in;
   break-even edge ~4.5pp. Taker strategies need a large fast edge.
2. Makers trade free and get rebates; durable edges are most likely maker.
3. The fee ~ p(1-p) vanishes at extremes: taker trades at p>0.95 / p<0.05
   cost ~0.2-0.3c/share - the cheap zone.
