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
