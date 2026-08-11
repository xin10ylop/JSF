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
