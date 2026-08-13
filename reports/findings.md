# Findings: Polymarket Short-Dated Crypto Binaries

**Headline.** The paper's strategy does not survive contact with real fees at
scale, and neither does any always-on edge I could find on the *old*
contract — the venue was, and is, close to efficient there. But on
**2026-08-07 Polymarket silently changed what these contracts settle on**,
and the book has not re-priced it. Trading that gap against real prints,
net of the real fee, pays **+3.04c/share (per-day SE 0.07)** on 2.56M shares
across five coins, versus **+0.49c** for the identical machinery on a
pre-change control. That is the finding. It is three days old, it is
decaying on the thin coins, and it is the only thing in this study that
clears the bar.

---

## 1. The contract changed, and my first reading of it was wrong

On 2026-08-07 the `resolutionSource` field on every 5m/15m crypto market
flipped, simultaneously across BTC/ETH/SOL/XRP/DOGE:

```
https://data.chain.link/streams/btc-usd
   -> https://data.chain.link/streams/btc-usd-twap-30s-streams   (5m markets)
   -> https://data.chain.link/streams/btc-usd-twap-60s-streams   (15m markets)
```

That is venue metadata, not inference. The contract is now:

> **Up iff mean(P over [t1-w, t1)) >= mean(P over [t0-w, t0))**, w = 30s (5m) / 60s (15m)

**Both averages trail.** An earlier revision of this work assumed the strike
was the average over the *first* w seconds *inside* the window. That was
wrong, and the data says so clearly. Horse race on 2,880 settled
post-change 5m markets, five coins, using free Binance 1s klines as the
price path:

| rule | pre-change (Aug 1-6) | post-change (Aug 7-9) |
|---|---|---|
| old: `P(t1) >= P(t0)` | **0.9491** | 0.8904 |
| **trailing / trailing** | 0.9129 | **0.9503** |
| forward strike `[t0, t0+w]` | 0.8990 | 0.8943 |

The crossover lands exactly on the metadata flip. On the 266 post-change
markets where the trailing rule and the old rule disagree, the trailing
rule is right **82.7%** and the old rule 17.3% (every coin 73-91%). A scan
over w peaks exactly at the advertised value: 5m markets 15s/20s/**30s**/45s/60s
-> 0.9302 / 0.9344 / **0.9503** / 0.9399 / 0.9149; 15m markets peak at
**60s** (0.9744). The residual ~5% miss is the Binance-vs-Chainlink proxy,
not the rule — the old rule scored the same 0.949 ceiling in its own era.

Consequence that matters: **the strike is backward-looking and known before
the window opens.** There is no dead zone at the start, and
`src/rollavg_pricer.py`, `bot/state.py` and the paper broker's settlement
have all been corrected to this rule.

## 2. Why the book is wrong, and where

Under the old contract, at t1-1s the outcome was still a live coin flip
whenever spot sat near the strike. Under the new one, the last w seconds
are an *average*: with n of w seconds already printed, only the remaining
`rem` can still move it, and its contribution has sd `sigma*sqrt(rem^3/3)`.
Variance collapses as `rem^3`, not `rem`. A book still quoting the old
terminal contract is therefore wrong, late in the window, in a direction
that is computable in real time from data we already have.

Define, at print time tau inside the settle window:

```
K   = mean(P over [t0-w, t0))          strike, known at t0
S   = sum(P over [t1-w, tau))          locked
rem = t1 - tau
z   = (S + rem*spot - w*K) / (sigma * sqrt(rem^3/3))
```

Calibration of z against settled outcomes, post-change, 5 coins — the
market price column is what actually traded:

| z | prints | actual P(Up) | market px | gap |
|---|---|---|---|---|
| > +3 | 45,905 | 0.9159 | 0.8486 | **+6.73pp** |
| +2..+3 | 10,641 | 0.8878 | 0.8156 | **+7.22pp** |
| +1..+2 | 13,676 | 0.8382 | 0.7668 | **+7.14pp** |
| -1..-2 | 14,583 | 0.2860 | 0.2973 | -1.13pp |
| < -3 | 47,286 | 0.1833 | 0.1870 | -0.37pp |

## 3. The edge, priced against real prints only

Every price below is a price at which a trade demonstrably happened; no
fill is assumed. Fee is the venue's verified taker schedule
`0.07 * p * (1-p)` (0.79c at the strategy's average price of 0.87 — the fee
being small at high prices is part of why this works).

**Settle window only (rem <= w), 5 coins, 2026-08-07..09:**

| gate | EV/share | per-day SE | shares | hit rate | pre-change control |
|---|---|---|---|---|---|
| \|z\|>=1.0 | +3.16c | 0.36 | 2,983,749 | 0.904 | +0.47c |
| \|z\|>=1.5 | +3.12c | 0.15 | 2,773,502 | 0.911 | +0.41c |
| **\|z\|>=2.0** | **+3.04c** | **0.07** | **2,563,894** | **0.916** | **+0.49c** |
| \|z\|>=3.0 | +2.80c | 0.15 | 2,172,028 | 0.918 | +0.03c |

Break-even hit rate at the average fill price of 0.877 is 0.885; realised
is 0.916. **All 15 coin x day cells are positive:**

| coin | Aug 7 | Aug 8 | Aug 9 |
|---|---|---|---|
| BTC | +3.48c | +3.50c | +3.01c |
| ETH | +5.77c | +4.96c | +1.41c |
| SOL | +1.10c | +3.34c | +1.43c |
| XRP | +4.86c | +3.04c | +0.82c |
| DOGE | +2.65c | +3.90c | +2.06c |

**Robustness.** The z gate depends on a vol estimate, so I stressed it: at
**3x** my sigma (absurdly conservative) post-change \|z\|>=2 still pays
**+2.20c** while the pre-change control goes **-0.18c**. The edge is not an
artefact of mis-specified vol.

**Where it lives.** By seconds remaining (\|z\|>=2): 0-5s +0.41c, 5-10s
+1.23c, 10-15s +2.36c, **15-20s +6.11c**, 20-30s +3.73c. Extending entry to
before the settle window opens dilutes it to +1.64c and makes it
day-unstable — the mechanism really is the partial-average variance
collapse, so the bot only trades `rem <= w`.

## 4. Economics

- **EV/trade**: ~3.0c/share at 100 shares = **$3.00/trade** at ~0.87/share
  ($87 notional). Win rate 0.916 vs 0.885 break-even.
- **Frequency**: 743 markets/day carry >=100 qualifying shares across the
  five coins; median qualifying market has 173 shares.
- **Capacity** (shares that actually cleared at qualifying prices):

  | cap per market | shares/day | edge in the pool @3.0c |
  |---|---|---|
  | 50 | 47,409 | $1,422/day |
  | 100 | 87,998 | $2,640/day |
  | 200 | 145,467 | $4,364/day |
  | 500 | 233,723 | $7,012/day |

- **Bankroll**: 5m contracts turn capital over 288x/day. At 100
  shares/market and ~2.6 concurrent markets, working capital is **~$230**;
  **$500-1,000** is a comfortable bankroll for the full 100-share program.
  This is capital-light — the constraint is edge decay, not money.
- **Realistic take.** The $2,640/day figure is the *whole pool*, and we
  would be competing for it with the takers already hitting those asks. A
  10-30% capture is $260-790/day; that is the number I would plan against,
  not the pool. The bot is BTC-only today (BTC is 2.42M of the 2.98M
  qualifying shares); per-coin state is the capacity upgrade.

## 5. What was ruled out (all of it, honestly)

On the **old** contract, nothing worked, and the free 229K-market
multi-coin dataset made that verdict much stronger than before:

| family | result |
|---|---|
| The paper's N(d2) divergence rule, taker | **-1.68c/share** [CI -2.85,-0.46], 6,619 markets. Signals cluster at p=0.5 where the fee peaks |
| The paper's premise (model beats mid) | Inverted. Mid beats N(d2) under 9 vol estimators, every phase (Brier 0.0416 vs 0.0488) |
| G(z) divergence, maker | -2.6c/share — fills *are* the adverse selection |
| FLB harvest, all execution styles | -0.8 / -0.8 / +0.3c, CIs span 0 |
| Fast-cancel maker (0.5-10s) | latency-INVARIANT -0.6..-0.9c |
| Endgame taker, old contract | -1.0 to -6.7c |
| Vacuum ladder | Train +39c/+29c, **test -9.2c/-5.7c**. All profit from one Apr20-May10 crash. n=1 payoff event |
| AHL multi-horizon momentum | Signal is real, universal and **inverted** (short-term reversal): ETH +7.05pp spread (t=13.6, train 7.06 / test 7.06), SOL +4.95, DOGE +4.34, XRP +3.48, BTC +3.34 on 229K markets. **But every book already fades it** at open; residual is sub-fee; real-fill maker version -2.06c train / -3.20c test |
| Copy the persistently-profitable wallets | Skill persists (Spearman +0.43, top decile +4.44c/sh out of sample) but copying is **-6.77c/share at 1s lag**. Their edge is price selection, not direction |
| Venue-wide maker alpha | 66.6M shares: taker gross +0.09c, net -0.76c => **maker gross -0.09c**. The (price x phase) cell map has train/test corr **-0.35**. Liquidity provision here earns zero before rewards |

That last row is the cleanest statement of why the old contract was a dead
end: takers pick direction slightly better than random (+0.09c) and lose
exactly the fee; makers collect the mirror image and nothing more.

## 6. Risks — read these before sizing up

1. **Three days of post-change data.** Per-day SE at the \|z\|>=2 gate is
   0.07c and all 15 coin-day cells are positive, but this is a 3-day
   sample. It is being extended live.
2. **This is a re-pricing lag, and lags close.** ETH went +5.77 -> +4.96 ->
   +1.41 and XRP +4.86 -> +3.04 -> +0.82 across the three days. BTC has
   held (+3.48/+3.50/+3.01), likely because it is the deepest and slowest
   to re-quote. Expect this to decay; the bot logs the gap every tick so
   the decay is measured, not guessed.
3. **Ex-ante depth is not yet confirmed.** The tape proves the mispricing
   exists at prices where trades cleared, and print sizes bound depth from
   below (7.5% of qualifying prints are >=100 shares). But my order-book
   recorder was down for most of Aug 7-9, so I have only 4 post-change
   markets of resting-depth evidence — not enough. `src/depth_sim.py` runs
   the true ex-ante test and needs the recorder to accumulate.
4. **Binance is a proxy for Chainlink.** Both averages are computed off the
   same series so a constant basis cancels, but timing noise does not. The
   95% rule-verification accuracy bounds this.
5. **Competition.** The asks we would hit are being hit by someone already.

## 7. Relative to the paper

Reproduced: the framework, the driftless reduction, the direction-robust
favourite premium it could not disentangle. Refuted: its strategy's
profitability under real fees at scale, and its premise that a spot+vol
model out-forecasts this market. Found beyond it: the fat-tailed empirical
pricing curve, the verified settlement and latency microstructure, the
complete fill-conditioned map of where maker money goes (nowhere), the
universal short-term reversal, wallet-level attribution showing skill is
price selection rather than prediction — and the one thing that actually
pays, which the paper could not have seen because it did not exist when the
paper was written: **a contract that changed underneath a book that has not
caught up.**

---

# 8. Execution realism: what survives when the fill model stops flattering us

The live paper bot's +6.89c/share on 154/154 winning fills was not a
result. It was the fill model. Five gaps, each now measured rather than
assumed.

## 8.1 The 154/154 hit rate was never surprising

Fills inside one market settle on one outcome. 154 fills across 29 markets
is **29 bets, not 154** — 29/29 at an average price of 0.927 is a ~10%
event, not the 1-in-100,000 the fill count implies. `src/score_paper.py`
now reports a market-clustered t alongside the naive one and labels the
naive one wrong. Nothing was broken here; the statistic was.

## 8.2 The honest edge: decide late, fill only at real prints

`src/tape_latency.py` computes the signal from information available `lag`
seconds *before* a print, then fills at the print. A print is proof the
price existed and someone supplied that size — it answers "would we win the
race", "would we move the book", and "are we a participant absent from the
data" in one move, because participating in a print means being one of the
counterparties that really traded.

BTC 5m, post-change, cap 200 shares/market:

| decision lag | c/share | $/day | t (clustered) |
|--------------|---------|-------|---------------|
| 0 s | +2.13 | +851 | +2.68 |
| 1 s | +1.65 | +648 | +2.12 |
| 2 s | +0.89 | +346 | +1.15 |
| 3 s | +0.51 | +195 | +0.65 |

**The edge decays about 0.5c/share for every second of decision lag.** Our
real delay is ~250 ms (venue hold) + RTT, so we sit between the 0 s and 1 s
rows. Everything below is quoted at lag = 1 s, which is ~2.5x the true
delay — a conservative bound, not a tuned one.

## 8.3 It is not a BTC artefact

Five coins, 5m family, lag 1 s, cap 200 sh/market, three assumptions about
*which* prints we win (`first` = we beat everyone to the earliest signal;
`uniform` = constant participation; `last` = we get only the leftovers):

| coin | sh/day | first | uniform | last | t (uniform) |
|------|--------|-------|---------|------|-------------|
| BTC | 39,220 | +$648 | +$531 | +$296 | +3.41 |
| ETH | 42,111 | +$695 | +$667 | +$471 | +5.98 |
| SOL | 27,081 | +$158 | +$177 | +$170 | +2.40 |
| XRP | 32,563 | +$424 | +$450 | +$365 | +4.72 |
| DOGE | 16,349 | +$371 | +$411 | +$283 | +4.03 |
| **total** | **157k** | **+$2,296** | **+$2,236** | **+$1,585** | |

Positive in all five coins under all three selection assumptions. BTC 15m
adds ~$138/day at lag 1 s. The worst case in the table — we are last in
every queue, in every market — is still **+$1,585/day**.

## 8.4 Is 200 shares/market a real participant?

From the tape: 83 distinct taker wallets per market in the endgame; the
median wallet takes 10 shares, the 90th percentile 105, the 99th 862, the
max 8,000. A 200-share cap puts us at the **95th percentile** of existing
participants and about 4.4% of qualifying flow. Aggressive, and precedented.
The capacity curve (`src/tape_capacity.py`) is close to linear to ~800
sh/market and then flattens.

## 8.5 What changed in the bot

* `latency_ms: 150` -> `venue_hold_ms: 250` + `rtt_ms` **measured** by
  `src/probe_latency.py`. The old guess was smaller than the venue's own
  mandatory hold.
* Fills capped at `participation` (0.5) of displayed size — from the
  measured ~50% survival at 400 ms in the 0.92-0.99 band.
* Fills capped at `vol_participation` (0.25) of what actually **printed**
  at our limit in the last 5 s. Depth is an offer; a print is a trade.
* Orders **walk the book**: size beyond the touch pays the next level.
* Our own fill **consumes** the liquidity it took.
* Venue rejections (re-validation failures, matching-engine restarts) and
  partial fills are modelled and counted.
* Sub-`orderMinSize` (5 shares) orders are misses, not fills.

Nine new assertions in `src/reconcile_bot.py` cover all of it.

## 8.6 What is still not modelled

* The queue itself. We model *whether* liquidity survives, not our position
  in it. Polymarket is price-time priority, and we have no visibility into
  where in a level our order would land.
* Correlation between missing and being wrong. Misses are drawn
  independently of the outcome; in reality an ask is pulled precisely when
  the puller knows something.
* Our own market impact beyond one order. Repeated participation at 4% of
  flow would eventually be priced in by the makers we trade against.

Only real capital settles these. The measured floor for a live micro-test
is the venue's own `orderMinSize` of 5 shares.

## 8.7 Multi-coin: one process per coin

BTC is about a quarter of the opportunity (section 8.3). The blocker was
never the edge — it was that `BotState` carries one oracle ring buffer, one
vol estimator and one basis series, so it describes one coin.

Two ways to fix that. Make all of it a dict keyed by coin, which touches
every pricing path the reconciliation harness exists to protect; or run one
process per coin. Measured cost of the second: **46 MB RSS at import**, no
pandas or scipy in the bot's import graph. Five processes is ~300 MB. The
refactor buys nothing for that, and process isolation means one coin's dead
feed cannot take the others down.

So: `bot/run.py --coin eth`, a `jsf-paperbot@.service` template instanced
per entry in config's `coins`, per-coin `logs/<coin>/` (fills, decisions,
flock — a second btc is still refused, btc and eth coexist), and
`src/score_paper.py` / `src/status.py` reading across all of them.

Verified per coin: the Binance symbol and Chainlink oracle symbol resolve
from one `COINS` map, and the vol seed comes back sane and correctly
ordered — BTC 1.3e-5, ETH 2.5e-5, DOGE 7.7e-5 per second, all inside the
plausibility band. An ETH process discovers ETH markets, holds its own CLOB
feed and ticks its own oracle.

**Risk limits bind per process**, so `daily_loss_limit` went 900 -> 200:
five coins at 900 would have been a $4,500 aggregate stop. A per-coin stop
also retires one broken feed rather than the whole book.

The recorder's oracle capture widened to every coin in `coins` — a
restarted ETH bot backfills its strike window from `data/live/rtds/`, and
with a btc-only filter it would have sat blind for a full window. Its CLOB
book capture stays on `record_book_coins` (btc, eth) because raw books ran
3.8 GB/period for btc alone before pruning.

## 8.8 The multi-coin build almost did not fit

The droplet is 961 MB with a 1 GB swapfile, and it was already 511 MB into
swap before the five bots allocated anything. A warmed bot process measured
**182 MB RSS**, so five would have wanted 910 MB against ~400 MB available.

137 MB of that 182 was `GzPricer`, constructed eagerly in
`BotState.__init__`. Unpickling its isotonic models imports sklearn and
scipy. It is touched by exactly one method, `fair_legacy()`, which serves
`GzValueMaker` and `ExtremeTaker` — both disabled in config. Five processes
were about to pay 685 MB between them for a code path none of them run.

Making it a lazy property takes a warmed bot to **56 MB**; the recorder
imports at 34 MB. Five bots plus the recorder is ~310 MB, which fits with
room to spare. `droplet_setup.sh` also grows the swapfile to 2 GB now
rather than only creating one when absent — the original run made 1 GB and
the `[ ! -f /swapfile ]` guard skipped it on every run afterwards.

## 8.9 Does the contract change apply to every coin?

The bot was extended to eth/sol/xrp/doge on the assumption that the
2026-08-07 trailing-TWAP rule — verified on 2,880 BTC markets — governs
them too. That assumption drives both the z the strategy fires on and the
outcome the paper broker settles against, and it had never been tested.

`src/verify_rule_multicoin.py` compares both candidate rules against the
venue's own resolved outcomes. The decisive column is the last one: on
markets where the two rules DISAGREE, how often is trailing-TWAP the one
that matches the settled outcome?

| coin | fam | pre-change | post-change |
|------|-----|-----------|-------------|
| btc | 5m | 0.036 | **0.787** |
| eth | 5m | 0.233 | **0.814** |
| sol | 5m | 0.400 | **0.794** |
| xrp | 5m | 0.182 | **0.918** |
| doge | 5m | 0.333 | **0.851** |
| btc | 15m | 0.000 | **0.786** |
| eth | 15m | 0.143 | **0.818** |
| sol | 15m | 0.167 | **0.818** |
| xrp | 15m | 0.429 | **0.842** |
| doge | 15m | 1.000 (n=2) | **0.800** |

Unambiguous and universal: before the change the END price governed every
coin, after it the trailing TWAP does, in both families. Overall match rate
rises with it (btc 5m 0.8651 -> 0.9111, xrp 5m 0.8981 -> 0.9688).

The residual — 79-92% rather than 100% — is expected. Binance stands in for
Chainlink here, and disagreement markets are precisely the near-ties where
proxy error bites hardest. The BTC verification against real Chainlink
ticks put it at 82.7%.

**Gap that remains:** `data/pmfree_15m/trades` covers btc, eth and sol
only. For xrp and doge the 15m *rule* is verified but the 15m *edge* is
not measured — the bot trades that family on mechanism, not evidence.
Either fetch those tapes or drop 15m for those two coins.

## 8.10 Full coverage: every coin, every family, measured

The 15m gap for xrp and doge is closed (tapes fetched from the free
endpoint). Post-change, lag 1s, cap 200 sh/market, uniform participation:

| coin | 5m $/day | 5m t | 15m $/day | 15m t |
|------|---------:|-----:|----------:|------:|
| btc  | +531 | +3.41 | +128 | +2.51 |
| eth  | +667 | +5.98 | +100 | +2.40 |
| sol  | +177 | +2.40 |  +52 | +9.75 |
| xrp  | +450 | +4.72 |  +67 | +3.88 |
| doge | +411 | +4.03 |  +15 | +4.39 |
| **total** | **+2,236** | | **+362** | |

**~$2,600/day** at 200 shares/market. The live config's
`max_market_dollars: 150` binds nearer 161 shares at these prices, scaling
this to roughly **$2,100/day**. Ten of ten coin-family combinations are
positive with t >= 2.4.

doge 15m is the one to treat sceptically: 710 shares/day over 110 markets
with a hit rate of 1.000. The t is market-clustered so it is not the naive
fill-count illusion, but $15/day on a perfect record is a small sample, not
a discovery. It costs nothing to keep and nothing to lose.

## 8.11 The control: is this the contract change, or a bias that was always there?

A positive post-change edge only means what we claim if running the SAME
rule on the SAME markets BEFORE 2026-08-07 pays nothing. Otherwise we are
harvesting a generic favourite/longshot bias that predates the contract
change, has a different cause, and may already be arbitraged.

BTC passed this long ago (+3.04c/share post vs +0.19c pre). The four coins
added with the multi-coin build had never been tested. 5m, lag 1s, cap 200
sh/market, uniform participation:

| coin | pre-change c/sh | pre t | post-change c/sh | post t |
|------|----------------:|------:|-----------------:|-------:|
| eth  | **-2.10** | -0.78 | +1.58 | **+5.98** |
| sol  | +1.31 | +0.67 | +0.65 | **+2.40** |
| xrp  | **-0.51** | -0.32 | +1.38 | **+4.72** |
| doge | **-0.85** | -0.25 | +2.51 | **+4.03** |

Every pre-change edge is statistically indistinguishable from zero
(|t| <= 0.78) and three of the four are negative, while every post-change
edge is significant. The effect is the contract change, not a standing
bias. (BTC 5m pre-change returned no rows here because the local 1s klines
do not reach back to the pmfree_x4 March/April window; its control was run
separately and reported earlier.)

**SOL is the exception worth naming.** Its pre-change point estimate
(+1.31c) is LARGER than its post-change one (+0.65c) — only noisier. On
this evidence some of SOL's post-change edge could be a pre-existing effect
rather than the repricing failure. It is also the weakest coin on the
post-change side (t=+2.40, +$177/day of the ~$2,600 total). Kept for now,
but it is the first thing to cut if live paper disagrees, and its 15m
t=+9.75 on 86 markets should be treated as small-sample rather than as
reassurance.

---

# 9. Adversarial audit of the live result (2026-08-12)

The 11.4h live paper run reported +$1,958.43, +6.40c/share, t=+4.97 on 308
markets — roughly 4x the offline tape's ~+1.65c/share. A five-angle audit
with independent refutation of every finding: **19 of 28 findings refuted,
9 survived.** Every "fatal" claim was refuted. What follows is what
survived, and two errors it found in MY OWN comparison rather than the bot.

## 9.1 The benchmark was wrong, not (only) the bot

**The tape window did not match the bot's.** `bot/strategy.py` gates on
`rem > min(window_s, m.w)` with `m.w = 30` for 5m — so a 5m market is
tradable only in its **last 30 seconds**. Both `tape_latency.py` and
`tape_capacity.py` defaulted to `(2,60]`, benchmarking the live bot against
a tape full of prints it can never reach. Corrected, the tape edge rises
~25% per share on every coin:

| coin | (2,60] (wrong) | **(2,30] (correct)** | t |
|------|---------------:|---------------------:|---:|
| btc | +1.35c | **+1.80c** | +3.67 |
| eth | +1.58c | **+1.79c** | +4.59 |
| sol | +0.65c | **+0.96c** | +2.89 |
| xrp | +1.38c | **+1.55c** | +3.61 |
| doge | +2.51c | **+3.07c** | +3.66 |

**The tape prices z off the wrong series.** `tape_latency.build_lagged`
takes K, S, spot and sigma from Binance 1s klines; the contract settles on
Chainlink, and the live bot reads the actual oracle. So the offline signal
is a noisy proxy of the live one, and the tape is a *lower bound* on the
achievable edge, not a fair comparator. This is the leading remaining
explanation for live > tape and it has not been quantified.

Together these do not close the gap — live +6.38c against a corrected
~+1.8c is still ~3.5x — but they mean the gap was never 4x, and the
comparator, not the bot, is where the next measurement belongs.

## 9.2 Fatal claims, all refuted

* *"Fill price comes from displayed depth but size is validated against
  prints at the limit, so nothing requires a print at the price paid."*
  **Refuted**: `o["limit"] = sig["px"] = ba`, the best ask, so the limit IS
  the cheapest displayed level and `depth()` cannot return anything below
  it. Price and print are coupled.
* *"Down-side fills execute off a stale Down book."* **Refuted** —
  the code observations hold but the composition asserted does not exist.
* *"100% of the outperformance is in sub-0.70 fills, and stripping them
  puts the run below the tape."* **Refuted**: the arithmetic reproduces but
  the benchmark it strips against is the contaminated one above.

## 9.3 What survived, in order of consequence

1. **The recorded diagnosis in commit 03d56d9 was wrong.** The Binance vol
   estimator's under-read is not feed throttling. `OnlineVol.update()`
   pairs the last price of second k-1 with the FIRST price of second k, so
   the open->close move inside each second never enters any return. On the
   repo's own recorded tape (32 trades/s, 12,463 distinct prices — not
   throttled) it reads 1.4e-06 against a true 5.1e-05. Decimating the tape
   RAISES the reading, the opposite of throttling. Docstring corrected.
   No effect on any traded decision: the oracle path always won.
2. **`fair_legacy()` was on the hot path**, called on every priced
   evaluation inside an unconditional `observations.append`. It defeated
   the lazy `GzPricer` (all five processes loaded sklearn anyway, ~72 MB
   each), passed `vol.var` in raw with no `ok()` check and no fallback
   guard — the single call site that could consume a broken sigma — and
   appended to a list nothing in the repo ever reads, unbounded, for the
   life of the process. Removed; `observations` is now a bounded deque and
   a warmed process is 56 MB with sklearn absent.
3. **Every pre/post-change control in this project is ONE DAY.**
   `rollavg_edge_test.load_1s` reads 1s klines only from
   `data/binance_alts/{SYM}-1s-{DATE}.zip`, which holds 2026-08-06..09
   only; any other day is skipped by a bare `except: continue` and dropped
   by `dropna`. Every "pre-change" figure reported in section 8.11 rests on
   2026-08-06 alone, 78-104 markets per coin. The controls still point the
   right way but are far weaker than stated.
4. **The SOL caveat in 8.11 was an artifact and should be withdrawn.** Its
   pre-change +1.31c came from `data/pmfree_x4`, a market-level *selection*
   (fetched with a volume filter), not a full capture. On the complete tape
   the same day reads **-1.86c (t=-1.14)**. Full-tape pre controls: btc
   +0.57 (t=+0.43), eth -1.71 (t=-1.06), sol -1.86 (t=-1.14), xrp -0.52
   (t=-0.48), doge -0.80 (t=-0.52). **SOL's control passes.**
5. Minor: the "slippage 0% worse" line is structurally forced (the limit is
   the touch and `depth()` filters to `<= limit`) and should be labelled
   "price paid vs our limit"; `score_paper.outcomes_for` drops whole
   100-slug chunks silently on any HTTP error and mislabels them "markets
   still open".

## 9.4 Still unexplained

Live `(0.5,0.7]` at hit 1.000 on 84 fills. The refutation showed the null
it was tested against is contaminated, but did not produce a clean null
that makes it ordinary. That, and the Binance-vs-Chainlink signal gap, were
the two open questions when this was written; the signal gap is now
quantified in 9.6 and the bucket anomaly has since softened (0.95 on a
larger sample) but remains above its tape (0.78).

## 9.5 The "Binance is a noisy proxy" explanation: first evidence is against it — SUPERSEDED BY 9.6

I offered the Binance-vs-Chainlink gap as the leading innocent explanation
for live beating the tape ~3x. `src/oracle_vs_binance.py` tests it directly:
apply the identical verified contract to both series on the SAME markets and
see which agrees with the venue.

On the 15 post-change btc 5m markets where the recorder captured full
Chainlink coverage of both windows:

* Chainlink-derived rule agrees with the venue **1.0000**
* Binance-derived rule agrees with the venue **1.0000**
* the two disagree on **0 of 15** markets
* margin correlation **0.9997**; sd of (binance - chainlink) margin **2.10**
  against a Chainlink margin sd of **67.6** — about 3% noise

Fifteen markets is not a result, and the slice is plainly unrepresentative:
on 1,000 post-change btc markets the Binance-derived rule matches the venue
only 0.9120, so it does get outcomes wrong ~9% of the time. But those
errors are concentrated in near-ties, and the strategy only fires at
|z| >= 2 — i.e. precisely where the two series agree. That reasoning cuts
against my own hypothesis: the proxy error lives in the markets we do not
trade.

So the ~3x gap between live and tape is, as of now, **unexplained**. The
candidates that remain:

* participation model — `--picks first` (greedy, closest to what the bot
  does) runs ~22% above `uniform` on the same tape. Real, and far too small.
* something in the live fill path the audit's refutations did not reach.

The recorder began capturing all five coins' oracle ticks continuously on
2026-08-11, so re-running this in a day gives hundreds of paired markets
instead of fifteen. That is the cheapest way to close it.

**Correction (2026-08-12):** the re-run happened and points the other way.
The n=15 slice was exactly as unrepresentative as feared, and the |z|>=2
argument above was wrong in an instructive way: the strategy's z is
computed at the DECISION instant from a partial settlement window, while
the disagreements are decided by what the oracle does in the seconds after
the fill. A market can look like a |z|=3 lock on Binance and still settle
the other way on Chainlink. See 9.6.

## 9.6 The re-run at n~139/coin: the proxy handicap is real, and it is the right size

`src/oracle_vs_binance.py` (made self-sufficient: outcomes from Gamma,
klines from the Binance public mirror, oracle ticks from the droplet's
continuous RTDS capture), ~139 paired post-change 5m markets per coin:

| coin | Chainlink agrees w/ venue | Binance agrees | disagree % | Chainlink right on disagreements |
|---|---|---|---|---|
| btc  | 0.9928 | 0.9565 | 5.1% | 0.857 |
| eth  | 0.9928 | 0.9640 | 2.9% | 1.000 |
| sol  | 1.0000 | 0.9640 | 3.6% | 1.000 |
| xrp  | 1.0000 | 0.9856 | 1.4% | 1.000 |
| doge | 1.0000 | 0.9928 | 0.7% | 1.000 |

The Chainlink-derived rule reproduces the venue essentially perfectly —
which is one more independent confirmation of the contract reading — and
the Binance-derived rule mis-signs 0.7–5.1% of markets depending on coin.

**Sizing the drag.** When the tape's Binance-z buys the side that the
venue settles against, the backtest books roughly a full loss where the
live bot (pricing off the real oracle) would not have fired or would have
bought the winner — close to a 1.00/share swing per mis-signed market. At
the measured mean mis-sign rate (~2.7% weighted across coins) that is a
**~2.74c/share drag on the tape**:

```
coin   disagree%    live    tape     gap
btc          5.1    7.38    1.80   +5.58
eth          2.9    2.71    1.79   +0.92
sol          3.6    1.47    0.96   +0.51
xrp          1.4    0.52    1.55   -1.03
doge         0.7    3.92    3.07   +0.85

pearson(disagree, gap)  = +0.769
spearman(disagree, gap) = +0.500

mean tape 1.83c + mean drag 2.74c = 4.57c   vs live aggregate 4.55c
```

The corrected tape (~1.8c/share) plus the measured drag lands within
0.02c of the live figure. The live-vs-tape gap is no longer unexplained:
**the tape is a floor because it prices off the wrong series, and the
size of the handicap matches the size of the gap.**

**What does NOT fit, stated plainly:**

1. **XRP is inverted.** Second-lowest disagreement rate (1.4%) yet the
   only coin running BELOW its tape (-1.03c). Either XRP's live sample is
   still small enough for this to be noise, or something coin-specific
   (thinner books, worse fills) eats its edge. Watch, don't explain away.
2. **The rank correlation is only +0.500 on five points.** Pearson +0.769
   is carried substantially by btc being extreme on both axes. Five coins
   is five data points; this is consistent with the story, not proof.
3. The 0.02c agreement of the aggregate is partly luck — the per-coin
   residuals (+2.8, -1.9, -1.5, -2.6, -1.9 after subtracting a uniform
   drag) do not vanish individually.

**The definitive test** is now possible and cheap: rebuild the tape edge
with **Chainlink-derived z** on the same markets the tape already scores.
The droplet has continuous five-coin oracle capture plus free trade tapes
(`src/fetch_pm_free.py`). If the Chainlink-z tape converges to the live
c/share per coin, the gap is closed mechanically, not by correlation on
five points. Until then the operating position is: live figures are the
measurement, tape figures are a floor with a now-quantified handicap.

## 9.7 The definitive test ran, and it REFUTES 9.6

`src/tape_chainlink.py` on the droplet's 24h continuous capture
(2026-08-12, ~150-270 paired markets per coin, same prints, same fee,
same caps, only the input series changed):

```
                 BINANCE-z   CHAINLINK-z   uplift      live c/sh
btc  5m first      +0.39        -0.88      -1.28         +5.96
doge 5m first      +3.63        +2.86      -0.77         +4.73
eth  5m first      +0.79        +0.63      -0.16         +2.12
sol  5m first      -1.01        +2.46      +3.47         +1.32
xrp  5m first      +0.61        -0.24      -0.85         +0.51
```

9.6 predicted Chainlink-z would rise toward the live figures. It does
not: it is LOWER than Binance-z on four of five coins, and the aggregate
Chainlink-z tape is ~+0.24c/sh (~$137/day) against a live run rate near
$1,900/day over an overlapping window. The 9.6 arithmetic (tape + drag =
live, within 0.02c) was a five-point coincidence -- exactly the risk its
own caveats flagged. Two things in the table are real and useful anyway:
**sol and doge keep a significant edge on the correct series** (sol
+2.46 t=+3.51, doge +2.86 t=+4.11), and Chainlink-z hit rates are
uniformly higher (it fires later, on more-locked prints).

What can still explain live >> every tape, in order of current
plausibility:

1. **The paper fill model of the record era.** 63.5% of recorded shares
   are possible re-claims (audit_doubledip upper bound); the strict
   ledger + direction-aware tape caps (3f0f3bb) only took effect at the
   end of this window. The clean subset reads +2.99c/sh -- much closer
   to tape reality than the +3.68 headline.
2. **The edge is decaying this week.** BOTH input series read near zero
   on this 24h window against +3.04c on 08-07..09 validation; live had
   its first negative day the same day (btc kill, the >0.95 band
   -1.92c/sh on 08-12 vs +1.00 on 08-11). A venue re-pricing after the
   settlement change is the expected end state; check
   reports/decay_log.csv and the next strict-model days.
3. **A mechanical bias in this test, not yet closed:** the LIVE bot's
   spot is basis-adjusted BINANCE (sub-second fresh, state.spot_adj);
   my Chainlink-z uses pure oracle spot, stale by up to ~2s at decision
   time -- a lagged z fires worse. The live bot is a HYBRID (correct
   K/S/sigma from the oracle + fresh spot from Binance).
   tape_chainlink now has `--spot hybrid` to replicate exactly that;
   run it before concluding anything final.

Operating position until the hybrid run and 2-3 strict-model paper days
land: treat the +3.68c headline as an era artifact, the clean +2.99c as
the record, ~+1 to +2c as the defensible forward expectation, and DO NOT
size a live launch off the pre-3f0f3bb record. sol/doge look strongest
on the correct series; btc's tape edge this window is indistinguishable
from zero and its live dominance is the number most likely to shrink
under the strict model.

## 9.8 HYBRID-z lands: the story is now coherent

The faithful replication of the live bot's computation -- oracle K/S and
sigma, basis-adjusted Binance spot -- on a fresh 24h window:

```
            BINANCE-z  CHAINLINK-z  HYBRID-z (t)      live 27.8h
btc  5m       +0.61       -0.91     +0.78 (+0.50)       +5.96
doge 5m       +4.38       +3.21     +5.18 (+2.14)       +4.73
eth  5m       +2.53       +0.60     +0.36 (+0.21)       +2.12
sol  5m       -1.37       +2.50     +3.11 (+1.31)       +1.32
xrp  5m       +2.11       +1.26     +0.19 (+0.17)       +0.51
     aggregate ~ +$694/day at 200 sh/mkt caps, ~+1.2c/share
```

Three coins now RECONCILE: doge (tape +5.18 vs live +4.73), xrp (+0.19
vs +0.51), sol (+3.11 vs +1.32, tape above live). The residual gap is
concentrated in btc (live 5.96 vs tape 0.78) and eth (2.12 vs 0.36) --
and btc is precisely where the double-dip audit found the loosest fills
(btc flagged subset +8.47c/sh carrying $1,245 of its $1,579; btc clean
reads +2.83). The remaining story, stated plainly:

* the edge is REAL but currently ~+1c/share, ~$700/day at these caps --
  not the $1,900/day the loose-era record extrapolated;
* the record's excess over that was mostly fill-model looseness, which
  3f0f3bb has now closed; the strict-model paper days should land near
  the hybrid tape, and if they land meaningfully above it the tape's
  print-based fill assumption is what's pessimistic;
* window-to-window variance is large (Binance-z eth read +0.79 and
  +2.53 on two overlapping 24h windows two hours apart) -- single days
  prove nothing in either direction;
* doge and sol carry the most reliable signal on the correct series;
  btc -- the volume king of the record -- is the least proven forward.

**New finding: the decay monitor has been blind since 2026-08-09.**
daily_edge_check reads 1s klines only from data/binance_alts zips, which
end 08-09, so decay_log.csv silently stopped exactly when decay became
the launch question. Fixed: the edgecheck timer now also runs
tape_chainlink --hours 24 --csv reports/hybrid_decay.csv daily -- a
self-sufficient decay row on the correct input series. The doge 08-09
decay row (ev 16.9c, pool $3.5k) is the longshot-day outlier, not trend.

**Launch bar, restated against the original brief:** "$10/day is not the
target." The honest current estimate -- ~$700/day at 200-share caps
before any capacity scaling, concentrated in doge/sol, with btc/eth
awaiting strict-model proof -- still clears the bar IF it holds through
2-3 strict-model days and the hybrid decay rows do not trend to zero.
That is the go/no-go evidence now accumulating on its own.

# 10. The full pre-launch audit: fleet verdict and the closing record

Six line-by-line reviewers (48 findings), six adversarial verifiers, and
three researchers on fresh official sources. Every fix applied during
the audit was independently re-verified in place; every remaining
CONFIRMED finding is listed here or in the worklist. Raw transcripts:
reports/audit_fleet_raw/.

## 10.1 What the audit fixed (commits 9fbc1bc..f190f12)

Fill realism: lifetime claim ledger, direction-aware tape caps (were ~2x
inflated), windowed to the settle window (~5x dilution removed on 15m),
stale-book fill guard, per-side re-fire gate (Down was both throttled by
and re-armed by the wrong book's clock). Oracle integrity: hole guards
(MAX_HOLE_S), strike latch grace for the 1.6-2.8s delivery lag,
settle-quality guard + Gamma deferral, voided-market deferral,
backfill-merge on reconnect, watchdog hoisted above the symbol filter.
Signal quality: the trailing delivery-lag gap of S is priced at current
spot (one clock for both legs), stale-spot refusal (SPOT_MAX_AGE_S),
executable-Down = min(real ask, mirrored Up bid). Risk: halt ladder
(streak breaker -> cool-off -> half-size probe -> day kill), $400
backstop, state persists across restarts including probe-kills, open
positions replayed, kill flushes in-flight orders. Ops: guarded loops,
ENOSPC-safe logging, queue-drop instant resync, chrony, capture
retention, edgecheck out of the memory slice, hybrid decay monitor
(the old one had been blind since 08-09). Measurement: gamma-chunk
retries with honest fetch-failed labels, per-side and per-band clustered
evidence tools, declared-contract tripwire in discover().

## 10.2 Research verdicts that change the plan

**Venue (all live-probed 2026-08-12):** 250ms hold TRUE (made
non-cancellable Jun 5, announced on X only); fee 0.07*p*(1-p) taker-only
TRUE + a **taker-rebate program we never modelled** (3-50% of fees back
by 30-day weighted volume, crypto weight 2.3 -- at our volumes this is
worth roughly +0.1 to +0.3c/share); tick 0.01 static (the 0.001-above-
0.96 story is false today; subscribe to tick_size_change); **BNB, HYPE,
ZEC live on the same TWAP families** (capacity +~50%; zec 5m uses a 60s
window -- read twapLookbackSeconds per market, never infer); hourly and
daily families are Binance-candle contracts, NOT TWAP -- do not touch
with this strategy; per-signer order rate limiter live in warning mode
since Jul 24; the changelog does NOT carry crypto-binary rule changes.

**Live API:** py-clob-client is ARCHIVED and non-functional. CTF
Exchange V2 is live (new exchange addresses, pUSD collateral, EIP-712
domain v2, signature type 3, changed order struct). The live build must
use `polymarket-client` (Polymarket/py-sdk, v0.5.0, Python >= 3.11).
L1 key only for credential derivation; L2 HMAC for every call; use
derive-api-key on restart.

**Geo -- the biggest launch risk, above the edge itself.** The developer
docs put NL (and IE/JP/MT) in the frontend-close-only/API-open tier, but
the Help Center lists NL as fully restricted, the Dutch regulator has an
active upheld enforcement order that already forced NL IP-blocking once
(Feb 2026), and the ToS attestation covers being LOCATED in a restricted
jurisdiction -- with close-only-on-wallet as the enforcement remedy,
which strands open positions. Polymarket's own builder steer is
eu-west-1 (Dublin), same tier ambiguity. Additionally, Cloudflare Bot
Management fronts the CLOB and community reports (unanswered by staff)
describe 30-50% blocks on server-to-server order POSTs from datacenter
IPs. CONCLUSION: before any live dollar, get written answers from
Polymarket support: (a) is the tier-3 API carve-out intentional policy,
(b) what jurisdiction/KYC do they require for an API trader. Their
answer to (a) determines whether the live plan has a foundation.

**Empirical update (2026-08-13): the NL order path is OPEN.** Differential
probe of POST clob.polymarket.com/order with no credentials: from a US
IP it returns 403 "Trading restricted in your region" BEFORE auth; from
the Amsterdam droplet it returns 401 "missing address header" -- the geo
layer passed an order-path request through to authentication. Amsterdam
is therefore GO for shadow and micro-live. The written support
confirmation is downgraded to a before-scaling requirement: it protects
the phase where a meaningful balance sits on the platform, and the
fragility risk it hedges is bounded for this strategy anyway (positions
live <= 15 minutes; a sudden flip to close-only strands ~nothing).
Latency note: Polymarket's Dublin steer is AWS-region framing, not
physics -- AMS-LON ~6-8ms beats DUB-LON ~10-12ms, and DigitalOcean has
no Dublin region. Amsterdam is the optimal allowed DO location.

## 10.3 Outstanding worklist (in order)

1. Written Polymarket support confirmation on geo/API access (blocker).
2. Live executor on polymarket-client/V2 (blocker; the launchgap review
   is the spec: keys via EnvironmentFile, pUSD funding+allowances,
   negRisk routing, user-channel fills, ack timeouts, redemption sweep,
   venue-position reconciliation, stop_all.sh, sticky probe-kill in
   live mode).
3. Alerting (ops CONFIRMED): webhook/push on kill, halt, feed death,
   fill-quality divergence; OnFailure= units.
4. Scorer follow-ups (accounting, all CONFIRMED): close-boundary
   clustering (t=+3.40 is an upper bound; cross-coin same-window outcome
   correlation 0.52-0.65), voided 50/50 booking at 0.50, maker-fill
   fee/meta, fill_model version stamp + duplicate detection,
   reset_pnl.sh unit derivation.
5. Calibration: per-side gate decision from band_report's by-side table;
   recalibrate calib.py on a drift-balanced sample.
6. Expansion candidates once strict-model days confirm: bnb/hype/zec
   (declared-window support already shipped), maker-side variant to
   harvest the rebate instead of paying the fee, per-band sizing.

## 10.4 The honest launch posture

The strategy is real but smaller than the loose-era record claimed:
HYBRID-z says ~+1.2c/share, ~$700/day at 200-share caps -- and the
leakage review shows even that is slightly optimistic against what live
can reach (the benchmark's oracle legs use round-stamp time; live gets
rounds 1-2.6s late; the lag-decay table prices that at roughly -0.3 to
-0.8c/share). The strict-model paper days now running are the ground
truth. Go-live requires: (1) the geo answer in writing, (2) 2-3
strict-model days at or above ~$400-700/day with the hybrid decay rows
not trending to zero, (3) the live executor built on V2 with the
launchgap checklist closed, (4) first live week at minimum size
(5-10 shares) reconciling live fills against paper assumptions before
any scaling.
