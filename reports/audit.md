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
| 5m family train | PASS: +28.5c/sh, day-CI [$1157,$5645] |
| Untouched TEST period (both families) | **FAIL: 15m -9.2c, 5m -5.7c/sh, CIs negative.** Weekly profile: all train profit from the Apr 20-May 10 crash episode. Strategy reclassified as unvalidated event harvester; see findings.md |
| Live paper fills vs backtest expectations | RUNNING (bot live since 02:40 UTC) |
| WS print completeness vs Telonex tape | SCHEDULED (after daily publish) |

## G. Print completeness (ws feed vs Telonex tape)

Measured 2026-08-06 against the published Aug-5 tape:

- **Old recorder architecture** (synchronous writes, single-cycle
  subscriptions; ran Aug-5 evening): captured **0-4% of in-window prints**
  across four reconciled 15m markets (36 of 936; then 0 of 492/912/828).
  Root causes: server "slow consumer" kicks from blocking disk writes +
  subscription cycling. THIS is the class of silent failure that would have
  made paper fills fictional - caught by this reconciliation.
- **New architecture** (queue-decoupled recv, batched writers; live since
  03:00 UTC): captures 2,600-5,700 trade events per 15m wall-clock window
  across ~4 subscribed markets x both tokens - consistent with full capture
  (typical tape: 500-950 prints/market-window). RTDS oracle capture also
  recovered from ~500/h to the full ~6,800/h rate.
- Definitive ratio vs the Aug-6 tape: scheduled for the next publication
  cycle. Paper-bot fills logged before 2026-08-06 03:00 UTC should be
  treated as unreliable (broken feed); fills after are on the fixed feed.

## H. Closeout status (2026-08-07)

- **Definitive per-market ws/tape ratio: BLOCKED on API quota.** The Telonex
  key hit its download limit (~290K files pulled for the research corpus).
  Procedure when quota resets: download trades for 2026-08-06 slugs
  btc-updown-15m-1786012200 / -1786013100 (+ overlapping 5m windows),
  restrict to the recorder-covered span 10:38:26-10:50:10 UTC, compare
  per-asset print counts/sizes vs data/live/clob/2026-08-06_10.jsonl
  (Up direct, Down mirrored 1-p). Standing evidence meanwhile: new-feed
  event volumes are consistent with full capture (S.G above).
- **Paper loop verified end-to-end on the fixed feed** (since 03:00 Aug 6):
  42 ladder orders, 38 maker fills from real prints, 3 settlements against
  the oracle; net paper P&L +$250 (one +$240 vacuum catch, one +$70, one
  -$60 full-ladder loss). Three settlements demonstrate the mechanics work;
  they say nothing about expectancy.
- **Operational note**: this research container restarts frequently and
  kills the processes; continuous measurement belongs on the droplet
  (deploy/droplet_setup.sh, systemd auto-restart).

## Section I — audit of the post-2026-08-07 endgame strategy (2026-08-10)

### I.1 Bugs this audit caught

1. **My own settlement rule was wrong.** The first reading of the Aug-7
   change assumed a FORWARD strike (mean over the first w seconds inside
   the window). A horse race on 2,880 settled outcomes says the strike is
   TRAILING (`[t0-w, t0)`): 0.9503 accuracy vs 0.8943 for the forward
   variant and 0.8904 for the old rule; 82.7% correct on the 266 markets
   where the trailing and old rules disagree. Corrected in
   `src/rollavg_pricer.py`, `bot/state.py`.
2. **The paper broker was settling on the PRE-change rule.**
   `run.py::_settle_result` compared a single end price to a single strike.
   Every paper P&L would have been mis-scored. Now reads both trailing
   averages off the oracle ring buffer.
3. **One second of look-ahead in the backtest.** Binance 1s klines are
   indexed by OPEN time, so the bar at second s closes at s+1. The spot
   lookup was peeking one second ahead — material when rem < 30s. Fixed by
   re-indexing to close time; the edge fell from +4.07c to +3.57c, and the
   corrected figure is the one reported.
4. **The live vol estimator was 22x too small after a restart.** A 300s
   halflife EWMA seeded from a single observation needs ~30 minutes to
   converge; two minutes after restart the bot had sd 6.07e-7 against a
   true 1.34e-5, which inflates z by 22x and makes every market look
   certain (observed live: `z=27.96, fair=1.0` against a book at 0.47/0.99).
   Three fixes: bias-corrected weight `max(alpha, 1/n)`, a warm start from
   public 1s klines, and a plausibility band (`OnlineVol.ok()`) that
   refuses to price outside sd in [1e-6, 5e-4]. **This was caught only
   because the health heartbeat logs every pricer input** — a silent bot
   and a bot trading on garbage look identical without it.
5. **A negative-span prefix sum** silently produced garbage S before the
   settle window opened. It was NaN-guarded (so results were never wrong),
   but it hid the pre-window region entirely; now handled explicitly.

### I.2 Backtest / live parity

`src/reconcile_bot.py` drives a real `BotState` with synthetic feeds and
asserts:

- strike matches the offline formula to 1.5e-11
- z and fair are bit-identical to the backtest across rem = 120..2s
  (worst |dz| 9.5e-11, worst |dfair| 2.1e-14)
- paper settlement uses the verified trailing-TWAP contract
- the strategy gate fires and the paper broker charges exactly
  `0.07*p*(1-p)`

All checks pass.

### I.3 What is still NOT verified

- **Ex-ante resting depth.** The edge is measured against prints that
  cleared, and print sizes bound depth from below (7.5% of qualifying
  prints are >=100 shares; median market carries 173 qualifying shares).
  But the order-book recorder was down for most of Aug 7-9, leaving only 4
  post-change markets of resting-depth evidence. `src/depth_sim.py` is the
  true ex-ante test and is waiting on recorder uptime. **Do not size up
  before this passes.**
- **Live paper fills.** Zero to date on the corrected build.
- **Only 3 post-change days**, and the alt coins are already decaying.

### I.4 Two more bugs, both caught live by the health heartbeat

6. **The oracle sum was not an integral.** The backtest runs on Binance 1s
   klines, where exactly one sample covers each second, so summing samples
   IS the integral over the settle window. The Chainlink RTDS feed does not
   tick at 1 Hz, so summing its ticks scales the locked partial average by
   the feed rate. Observed live: `z = -5322` with 23s remaining. Fixed by
   integrating piecewise-constant (each price held until the next tick) in
   `BotState._integral`. `src/reconcile_bot.py` now asserts z is invariant
   to feed rate: 3 Hz drifts 0.07%, 0.33 Hz drifts 2.74% (was ~3x error).
   The strike is now a time-weighted mean for the same reason, and requires
   real tick coverage rather than one stale tick held across the window.
7. **Two bot instances ran concurrently** after a restart, both appending to
   `logs/paper_fills.jsonl` — which would double-count every paper fill in
   the record the go/no-go decision rests on. `bot/run.py` now takes an
   advisory `flock` on `logs/bot.lock` and exits if another instance holds
   it.

Both were invisible in backtest and unit tests: the synthetic reconciliation
feed ticks at exactly 1 Hz, which is precisely the case where the tick-sum
bug disappears. The lesson recorded here is that **parity tests must vary
the thing the backtest holds constant.**

### I.5 Disk

The recorder writes ~6.5 GB/day of raw CLOB JSONL. `bot/prune.py` distils
each finished hour to the endgame snapshots the depth test needs and deletes
the raw file: measured 3.8 GB -> 928 KB of parquet (~3,000x) with no loss of
evidence inside the final 90s of any window. It runs hourly via
`jsf-prune.timer`.
