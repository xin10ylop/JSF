# Hypothesis Register

Rules: every hypothesis is tested on train data (chronological first 60% of
markets per family), promising ones on untouched test (last 40%). Every
result lands in reports/results_table.md. Fees: taker 0.07*p*(1-p)/share;
maker fee 0, rebate not credited. Entries must be executable against
quotes/depth existing at decision time.

## A. Paper reproduction & direct extensions
- H1 Driftless N(d2) + 2pt buffer (the paper's rule) on the full updown
  history: net edge after real fees is zero/negative.
- H2 Divergence-gate edge is regime-dependent (vol regimes).
- H3 Model-vs-market calibration flips by window phase.

## B. Venue bias / favorite-longshot
- H4 Longshots overpriced by price bucket; favorite premium inside spread+fee
  for takers; net for makers only.
- H5 FLB strengthens in the final third of the window.
- H6 Mispricing asymmetric by side (Up vs Down).

## C. Window lifecycle
- H7 Pre-window forward-start phase: fade deviations from 0.5.
- H8 First seconds after window open misprice while the strike propagates.
- H9 Deep-ITM underpricing near expiry; fee ~ p(1-p) vanishes there ->
  taker-viable.
- H10 Final-minute tape imbalance predicts resolution beyond moneyness.

## D. Cross-market structure
- H11 Up ask + Down ask < $1 (crossed book) frequency/depth/persistence.
- H12 Strike-ladder monotonicity violations in the above family.
- H13 Venue-implied vol term structure (5m vs 15m vs 4h) inconsistencies.
- H14 Ladder-implied density vs realized -> trade extreme-deviation strikes.

## E. Inputs (spot & volatility)
- H15 Estimator horse-race: which vol input makes N(d2) beat the market
  (Brier)? How much is estimator vs framework?
- H16 Student-t / jump tails vs Gaussian at 5-15m.
- H17 Binance leads oracle ~2.5-3.5s; end-of-window sniping survives the
  p(1-p) fee only at extreme prices - quantify the surviving region.
- H18 Momentum/reversal drift conditioning improves driftless FV.

## F. Venue flow & microstructure
- H19 Aggressor imbalance predicts resolution incremental to moneyness.
- H20 Large prints overshoot; fading them as maker beats adverse selection.
- H21 Who is stale in the last minute: quotes vs oracle vs Binance.
- H22 Overround patterns by lifecycle phase; post when book underrounded.

## G. Maker strategies
- H23 Two-sided quoting around model FV with inventory caps, phase-gated.
- H24 One-sided value posting at fair - k*sigma.
- H25 Near-certainty taker buys (p>=0.97) where fee ~ 0.2c (ties to H9).

## H. Horizon & family comparison
- H26 4h/hourly less efficient than 5m/15m (less bot attention).
- H27 Daily above family strongest FLB (lottery tickets).
- H28 Mispricing wider in low-liquidity hours (02-06 ET).
- H29 Cross-family relative value: hourly above ATM vs overlapping 15m updown.
- H30 Skip conditions: spread, depth, phase - admission region maximizing EV.

## Benchmarks (nulls)
Random side at quote; always-buy-favorite at ask; always-Up; always-Down;
BTC buy-and-hold over the window.
