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

### I.6 The estimator mismatch (the most dangerous bug so far)

8. **The live vol estimator was not the backtest's estimator.** The
   +3.04c/share measurement computes sigma as

       log(px).diff().rolling(3600, min_periods=600).std() * spot

   a one-hour TRAILING standard deviation of 1s returns. `bot/state.py`
   used a 300s-halflife EWMA instead. Different estimator, and live on the
   droplet it read **2.6e-6 against a two-day actual of 1.34e-5** -- 5x
   low. Sigma is the DENOMINATOR of z, so this inflated z 5x: the health
   log showed `z=19.13` at 60s remaining and `fair=0.9943` on a 15m market
   whose book was still near 0.5.

   Everything downstream of that would have been noise: the gate fires on
   |z|>=2, and a 5x-inflated z clears it constantly.

   Fixed: `OnlineVol` is now a 3600-second rolling window of 1s log returns
   taking the sample sd (ddof=1, matching pandas), warm-started by paging
   the public kline endpoint backwards until the full hour is filled.
   Live sd is now 1.855e-5 -- the right estimator and the right magnitude.

   **Why the earlier reconciliation missed it:** every parity check injected
   the SAME sigma into both the bot and the offline formula, which by
   construction cannot detect an estimator mismatch. `reconcile_bot.py` now
   drives a known path through the live updater and asserts the bot's own
   sigma equals `rolling(3600).std()` on that path (measured: 0.00% diff).
   Injecting a shared input hides exactly the bug you are testing for.

Running tally of bugs this audit caught: 8. Five of them (the paper-broker
settlement rule, the tick-sum vs integral, the duplicate instance, the cold
EWMA, and this estimator mismatch) would each on their own have made live
trading lose money while the backtest still looked correct.

### I.7 Two consistency corrections found by interrogating the feed itself

9. **Sigma was borrowed from the wrong series.** The backtest is fully
   self-consistent: path, both window averages and sigma all come from
   Binance 1s klines. The bot is not — it reads the two contract averages
   off the Chainlink feed (correct: that is what settles) but took sigma
   from Binance. Measured on recorded ticks, the oracle's 1s sd is
   **1.23x** Binance's (3.64e-5 vs 2.95e-5 over the same window, using only
   adjacent-second pairs — the feed misses ~38% of seconds and treating a
   3s move as a 1s return would inflate sd by sqrt(3)). Borrowing Binance's
   sigma therefore understated the denominator of z by ~23%, again in the
   overconfident direction. `BotState.sigma_rel()` now prefers an
   oracle-derived sd and falls back to Binance only if the oracle window is
   too sparse.

   Also checked and cleared: the RTDS feed publishes **spot rounds, not an
   already-TWAPed value** (lag-1 autocorrelation of its 1s returns is
   +0.24; a 30s TWAP would show ~+0.9 and an sd ratio near 0.18). So
   averaging the feed over the settle window reproduces the contract rather
   than double-averaging it.

10. **Phi(z) is not the fair value.** The Gaussian is badly overconfident at
    this horizon: measured on 122,709 real prints, z > 3 settles Up **91.6%**
    of the time, not the 99.87% Phi(3) implies. With Phi as `fair`, the
    bot's `fair - ask >= edge_min` test was vacuous — Phi saturates at 1.0
    the moment z clears the gate, so any ask below 0.98 passed. `bot/calib.py`
    now supplies the measured z -> P(Up) mapping, so edge_min binds on a real
    number and the bot declines asks that are rich against the *empirical*
    probability. Example: at z=2 the empirical fair is 0.863, so an ask of
    0.88 is correctly refused where Phi(2)=0.977 would have taken it.

Tally: 10 bugs/corrections. The recurring shape is worth naming — every one
of the last five came from the live system being *consistent with itself*
but inconsistent with the configuration the edge was measured in. Parity
tests that inject shared inputs cannot see that class of error; only
driving the real code path with a known answer can.

## Section J — GATE 1 (ex-ante resting depth): first read, 2026-08-10

Two blockers found before the 48h wait, either of which would have wasted it:

11. **`depth_sim.py` read data the pruner deletes.** It globbed
    `data/live/clob/*.jsonl`; `bot/prune.py` distils those to
    `data/live/books/*.parquet` and removes the raw file. On a
    long-running box — the only kind this test is for — gate 1 would have
    silently reported "no data". Now reads distilled parquet first, plus
    any not-yet-pruned raw hour.

12. **`depth_sim` tested a different region than the bot trades.** Its
    window defaulted to 60s and it used `Phi(z)` as fair, so it entered at
    42-56s remaining — outside the settle window, in the +1.64c
    day-unstable region. Defaults now mirror `bot/config.json` exactly
    (window 30s, zmin 2.0, empirical fair, no fair filter unless
    `--require-edge`).

### What the first read says

On 17-52 post-change markets of recorded book (about one hour — far too
few to conclude, recorded here so the 48h run has a baseline):

- **The flow is takeable.** Joining the tape to same-second book snapshots:
  85% of settle-window volume is BUY prints, and **67% of them land at or
  below the standing best ask**. Median print 0.910 vs median best ask
  0.920. The mechanism is not broken.
- **But the favoured side rests at 0.99 most of the time.** Restricted to
  |z|>=2 favoured-side snapshots, the median best ask is **0.990**; only 6%
  of snapshots carry >=10 shares at or below 0.97, and 41% of the time the
  best ask is above 0.97 so the bot declines outright. Allowing up to 0.99
  finds plenty of size (34% of snapshots have >=100 shares) but at a price
  that is negative EV against the empirical fair.
- **So the edge lives in transient dips**, not in standing liquidity: the
  ask sits at 0.99 and drops to 0.87-0.92 in the instants around a trade.
- With 1s polling the sim filled **1 market in 17**, 21 shares of 100.

### Change made in response

The bot evaluated on a 1-second timer, which walks past most of those dips.
`bot/run.py` now evaluates **event-driven on every book update** for any
market inside its settle window, with the 1s loop demoted to a safety net
for quiet books. Both paths call the same `_try_market`, so they cannot
drift apart.

**This is the open risk on the whole strategy.** The tape-based ceiling
($490/day BTC at 100 sh/market) assumes fills at print prices. If the
realised take rate stays near 6% at ~21 shares, the true figure is an order
of magnitude lower. Gate 1 is NOT passed; it needs the 48h with the
event-driven build.

### I.8 The paper record was hiding losses (caught from 6 live fill lines)

13. **Unsettleable positions were orphaned, not scored.** `_settle_result`
    returns None when the oracle ring buffer lacks coverage of [t1-w, t1)
    — after a restart, or a feed gap. The market was then popped from
    `state.markets` and the position stayed in the broker forever: no
    settle line, no P&L, silently absent from the record.

    Observed live on the droplet's first three fills. The bot's own log
    showed two wins, +$5.59. Scored against the venue's resolved outcomes
    the truth was **-$33.04**: the missing fill (Down @0.370, 100 shares)
    LOST -$38.63. A performance read taken hours later would have been
    biased by exactly the fills the bot could not score.

    Fixed two ways, because one was not enough:
    - `bot/run.py` now parks unsettleable markets in `self.pending`, retries
      the oracle every 30s, and falls back to the venue's own resolved
      outcome via Gamma after t1+120s. Anything still unresolved at
      t1+1h logs `settle_FAILED` loudly instead of vanishing.
    - `src/score_paper.py` scores the fill log **independently of the bot**,
      against Gamma. Paper performance should never depend on the bot
      settling itself correctly. This is now the tool of record.

14. **False alarm worth recording.** A fill at ask 0.970 against a logged
    `emp_fairD 0.817` looked like a -15c/share blunder. It is not: `p_up(z)`
    is a z-MARGINAL, pooled over all market-price states. Conditional on the
    book also quoting 0.97, the realised hit rate is **0.99**, not 0.91.
    Realised EV is positive in every fill-price bucket at the bot's gate
    (0.92-0.95: +4.84c; 0.95-0.97: +2.68c), and the 42% of shares where the
    marginal model says "negative" realise **+3.07c**. So `require_edge`
    must stay off, and the logged `ev_est` is an honest summary statistic,
    NOT a per-trade fair value. Do not turn it into a filter.

### I.9 Pre-unattended safety review

15. **Both sides of one market could be held simultaneously.** Position caps
    are keyed by `(slug, side)`, so the 300-share / $200 per-market limits
    apply to Up and Down independently. `z` can flip sign late in a window
    (as rem -> 0 the margin M can cross zero), which would have the bot buy
    Up at ~0.9 and then Down at ~0.9 in the same market: ~1.90 paid for a
    guaranteed 1.00 payoff, a structural loss no limit would catch.
    `bot/run.py` now refuses any entry on a side whose opposite is already
    held, and logs `blocked_opposite_side`.

16. **The kill switch never reset.** `Risk.on_settle_pnl` zeroed `day_pnl`
    on a date change but left `killed` set, so the first day that breached
    the DAILY loss limit stopped the bot permanently — in paper mode,
    silently ending the measurement run. Now cleared on rollover.

Bug tally: 16. Running the system and reading its output has found more
real defects than any amount of re-reading the backtest would have.

### I.10 Two defects found from an hourly check showing zero new fills

17. **The clob consumer was an unguarded fire-and-forget task.** When I made
    evaluation event-driven, `_on_clob` began calling `_try_market` inside
    `asyncio.create_task(consumer())` with no try/except. One exception
    there kills the task silently: asyncio does not propagate it, the
    process stays up so systemd reports `active`, the queue fills, and no
    book update is ever processed again. `m.book_us` then freezes, book age
    exceeds the staleness limit, and the 1s fallback stops trading too.
    Fills simply stop with nothing appearing wrong -- exactly the observed
    symptom. Now every event is isolated, the task is supervised and
    restarted, `_try_market` is guarded per market, and the health log
    carries `clob_evs / clob_errs / evals / eval_errs / signals / killed /
    day_pnl` so a stall is visible at a glance instead of inferred from
    missing fills.

18. **The bot discarded the Down token's order book.** `_on_clob` returned
    early on any Down-token book event ("tracked on the up token only") and
    the strategy priced the Down side as `1 - best_bid_up`. That is wrong
    on two counts: on Polymarket the Down side is taken by BUYING the Down
    token off its own ask, whereas `1 - bid_up` is the price for SELLING
    Up (which requires Up inventory); and the two books are separate and
    can diverge, so the mirror is only a proxy. Roughly half the paper
    fills so far were Down. `MarketState` now keeps both books and
    `best_ask_dn()` uses the real Down ask, falling back to the mirror only
    when the Down book is absent, tagging which was used (`dn_src`) so the
    two can be compared in the fill log.
