# Strategy & Bot Audit — Vacuum Ladder

Goal: prove the paper bot's behavior equals the backtest's assumptions, and
that both equal the real venue. Every row is a potential divergence between
"backtest profit" and "live profit"; status as of 2026-08-06.

## A. Signal-path parity (backtest vs live bot)

| Item | Backtest | Live bot | Status |
|---|---|---|---|
| Pricing curve | G(z) isotonic, data/gz_models.pkl (K=1.35) | same file, bundled bot/gz_models.pkl | IDENTICAL |
| Spot input | Binance 1s close x rolling-600s median(oracle/binance), lagged | Binance trade ws, basis deque(600) median | EQUIVALENT (live is finer-grained; both causal) |
| Vol input | ewma5m on strict 1s grid from 1s klines | OnlineVol EWMA hl=300s updated on 1s boundaries | EQUIVALENT (fixed: was per-trade, now per-second) |
| Strike | first Chainlink round ts >= t0 (verified 99.996% vs on-chain) | RTDS round capture, same rule | IDENTICAL; fails safe (no strike -> no signal) |
| Gate timing | fv at last grid point <= post time (10s grid) | fv at decision tick (1s loop), post at rem<=27s | live slightly fresher; same rule |
| Signal | fv >= 0.65 -> ladder that side, last 27s | same, config-driven | IDENTICAL |

## B. Execution-path parity

| Item | Backtest | Live bot (paper) | Status |
|---|---|---|---|
| Order type | resting GTC bids 0.10/0.20/0.30, size 100/level, expire at t1 | same | IDENTICAL |
| Fill rule | print < level -> full fill; print == level -> beyond queue_ahead=200 | same code shape in PaperBroker.on_trade_print | IDENTICAL |
| Tape completeness | Telonex mirrored tape (both books merged) | CLOB ws BOTH tokens, down-book prints mirrored as 1-p (FIXED — was up-token only, missing ~65% of prints) | FIXED; live-vs-tape completeness check PENDING (scheduled when Telonex publishes Aug-5/6 files) |
| Mint-cross fills | mirrored tape shows down-book buys as up-sells -> counted | down-book buy events mirrored -> counted | MATCHED |
| Settlement | on-chain result_id (ground truth) | end_px = first RTDS round >= t1 vs strike (verified convention; fixed end_px capture bug) | MATCHED (bot risk: RTDS outage at t1 -> no settle -> position stays until manual; flagged for live mode) |
| Fees | maker 0 (rebates not credited) | maker 0 | IDENTICAL; verify realized fees on first live fills |

## C. Assumptions that make backtest CONSERVATIVE (real < backtest risk)

1. Queue-ahead 200 shares at our level (queue=1000 gives identical results —
   fills are sweep-through; queue position irrelevant).
2. Fill size capped by historically printed volume at/through level — we
   never assume liquidity that did not trade. Standing bids would if
   anything have improved dumpers' prices and captured MORE.
3. Maker rebates (15-25% of taker fee pool) not credited.
4. Fills only counted while order is live (posted at rem=27s; real bot can
   post earlier in the window for hourly-family variants — not assumed).

## D. Assumptions that could make backtest OPTIMISTIC (watch in paper mode)

1. **Print visibility**: if the CLOB ws drops trade events, paper fills
   under-simulate but LIVE fills would under-execute the backtest too only
   if the venue itself fails; the tape-vs-ws completeness check (pending)
   quantifies ws loss.
2. **Competition**: other bots adopting the same ladder would split the
   dump volume. Backtest counts all printed volume up to our size. Monitor
   live fill rates vs backtest (10 filled markets/day at base expected).
3. **Self-impact at 10x size**: our $600/market bids at 0.10-0.30 might
   cause dumpers' UIs to show better prices and dump MORE (good) or
   attract front-runners bidding above our levels (bad). Unknowable from
   history; paper mode cannot test this either — only live scale-up can.
4. **Regime dependence**: profits concentrate in high-vol burst days
   (top-5 days = 86% of train PnL). A calm month underperforms the mean
   materially. This is variance, not bias, but it dominates short-window
   evaluation.

## E. Data-integrity checks already passed

- Settlement conventions verified against on-chain results: updown
  99.996% (47,590 mkts), hourly 100% (7,385), above 100% (46,320).
- No lookahead: all features stamped and lagged (oracle by broadcast time,
  vol to t-1s, basis lagged 1s, G(z) frozen on train).
- Oracle-gap days cannot fake wins (settlement uses on-chain result_id).
- Backtest reproduces across two implementations (ladder_backtest and the
  print-map decomposition agree once conditioning is matched).

## F. Validation gates

| Gate | Status |
|---|---|
| 15m train (72d) | PASS: +39.2c/sh, day-CI [$338,$1902] > 0 |
| Capacity 10x | PASS: +38.1c/sh unchanged |
| Queue sensitivity | PASS: invariant |
| Gate sensitivity (0.65/0.80) | PASS: both positive |
| Hourly family | NO EDGE (thin flow) — excluded from deployment |
| 5m family train | RUNNING |
| Untouched TEST period (both families) | PENDING (single reveal, frozen params) |
| Live paper fills vs backtest expectations | RUNNING (bot live since 02:40 UTC) |
| WS print completeness vs Telonex tape | SCHEDULED (after daily publish) |
