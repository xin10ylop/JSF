# Findings: Polymarket Short-Dated BTC Binaries

Status: interim (train-period verdicts; test-period reveal pending final
strategy freeze). Data: full trade tapes for five BTC families (Oct 2025 -
Aug 2026), Chainlink settlement feed (Apr-Aug 2026, 10.5M ticks), Binance
1s/100ms, 3,000-market order-book sample. Split: train < 2026-06-15, test
after; test untouched so far except where marked.

## 1. What we reproduced / refuted from the paper

The paper (Semenas 2026) claimed a +4.6pp edge over break-even for a
driftless N(d2) divergence rule on 182 trades over one two-day window, and
itself flagged: CIs spanning zero, profit concentration, directional
confound, no better calibration than the market.

At scale (6,619 markets, both directions, real current fees):
- **H1 REFUTED-as-profitable**: the paper's rule (taker at touch, 2pt
  buffer) nets **-1.68c/share [CI -2.85,-0.46]**. Signals cluster near
  p=0.5 where the 0.07*p*(1-p) taker fee peaks. Every taker null loses
  (favorite -2.08c, random -2.14c).
- **H15**: the market mid beats N(d2) under EVERY standard vol estimator
  (best model Brier 0.13422 vs market 0.13147; market wins every phase,
  crushes the final phase 0.0416 vs 0.0488). The paper's premise - model
  sharper than market - is dead at scale.
- **H18**: momentum drift conditioning adds nothing (driftless confirmed).
- **H16**: settlement tails are t~5 (P(z>3.09) = 10x Gaussian); an
  empirical isotonic G(z) replaces N(d2) properly. Still loses to the
  market mid as a forecaster.
- **Combination (mid+z)**: beats mid by a trivial 6e-5 Brier. The market
  is ~efficient in the mean. **The paper was looking in the wrong place:
  the exploitable structure is level shifts and flow events, not superior
  forecasting.**

## 2. Venue biases (measured, cluster-robust)

- **Favorite-longshot bias exists unconditionally**: favorite-space gaps
  (won - implied), 13,765 markets: +1.0 to +1.9pp across 0.6-0.97, both
  sides separately, strongest EARLY window (+2.1pp at 0.8-0.9), fading
  late. Hourly family shows it bigger (+1.9-2.6pp).
- **But it is not naively harvestable**: fill-conditioned maker alpha
  (every print = a maker fill) shows adverse selection eats the bias for
  join-the-touch strategies (-0.77c realized vs +1.55pp unconditional at
  0.8-0.9). The market's flow is Binance-informed; resting orders get
  picked off faster than they collect the behavioral discount.

## 3. The pocket map (fill-conditioned maker alpha, phase x price)

Positive pockets (15m family, replicated on hourly):
- early-window favorites 0.7-0.97: +1.6..2.3c/share
- mid-window favorites 0.8-0.97: +0.9..1.5c
- endgame (last ~13s): the big one - see below.
Negative sinks: the belly (0.3-0.7) mid/late/final; favorites in the final
phase (stale bids sniped via the 2.5-3.5s Binance lead over the oracle).

## 4. The endgame vacuum (the finding)

Decomposing the endgame pocket by (print price - model fair):
- fills near/below fair: adverse (-20c when takers cross below fair)
- **fills at gap > 0.40: $1.03M printed volume where takers paid ~0.835
  for contracts with model fair 0.068 - they failed 84%. Maker alpha
  +67.5c/share.** Mechanism: holders of the WINNING side panic-dump into
  post-flip book vacuums (UI cash-outs sweeping empty books); mirrored
  prints confirm sibling-book bids intercept this flow via mint-crossing.

**Strategy: model-gated vacuum ladder.** In the last ~27s, post bids at
0.10/0.20/0.30 on the side with G(z) fair >= 0.65. Orders rest to window
end; fills only happen in sweeps.

Train results (15m, 72 days, conservative fills, queue=200, size=100/level):
- +39.2c/share, +190% on stake, 1,979 fills on 717 markets
- win rates 58/60/62% at the 0.10/0.20/0.30 levels (breakevens 10/20/30%)
- **day-cluster CI on daily P&L [$338, $1,902], mean $1,054/day**
- capacity: 10x size -> +38.1c/share unchanged, ~$9.5K/day, CI [$2.6K,$17.7K]
- queue-position irrelevant (sweep-through fills) - no speed race
- gate-robust (fv>=0.80: +46c/share)
- risk shape: lottery-like; ~55% of days negative (small), top-5 days carry
  ~86% of profit; worst train day -$537 at base size. Max loss per market =
  $60 at base size.
- hourly family: no detectable edge (dump flow 10-20x thinner, CI spans 0).
- 5m family replication + untouched test period: PENDING (in flight).

## 5. Latency structure (measured)

Binance leads the Chainlink oracle round by ~2s, and the round is broadcast
~1.1s late: a ~2.5-3.5s information lead. It is worth 10% relative Brier in
the last 15s - and the venue's top-of-book already reflects it (the market
beats oracle-spot models in the final phase). The taker fee (max 1.75c at
p=0.5) makes classic latency sniping unprofitable in the belly; the
surviving expressions of the lead are (a) refusing to quote into it and
(b) the vacuum ladder's model gate.

## 6. What this venue actually pays for

Not forecasting (the crowd's mid is sharp). It pays for:
1. **Liquidity of last resort at panic moments** (the vacuum ladder).
2. Patient early-window favorite liquidity (small, queue-dependent).
3. Taking the other side of lottery longshots (blocked by fees for takers,
   partially open to makers).
