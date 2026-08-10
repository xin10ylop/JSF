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
| S8 | Fat tails: standardized settle moves, train | 21K 15m + 63K 5m windows | kurt 11-13, MLE t-df ~5.2; P(z>3.09) = 10x Gaussian | MEASURED - Gaussian N(d2) structurally wrong in tails |
| S9 | G(z) empirical pricing curve (isotonic, symmetrized, per rem bucket) | 800K train samples | final-30s: G(2)=0.909 vs Phi(2)=0.977 (7pp certainty haircut), shrinking with rem | MODEL BUILT |
| S10 | Binance-adjusted spot vs oracle-known spot for settlement prediction | 700K samples | Brier gain concentrated in last 15s: 0.0563 vs 0.0628 (10% rel) | MEASURED - price off Binance |
| H17a | Sniping EV upper bound (entry at oracle-fair, model space) | train windows | rem=5s, |dp|>0.05: 6% of windows, ~26c/sh before market reaction | UPPER BOUND - venue tape test pending |
| H18 | Momentum drift conditioning (trailing 5m, standardized) added to z | 700K synthetic windows, out-of-time val | no Brier gain (0.15936 -> 0.15935 best); larger betas hurt | REJECTED - driftless pricing confirmed |
| H2 | G(z) stability across vol regimes | same | fit-ALL beats regime-restricted fits on both regime slices | STABLE - no regime switching needed in pricer |
| H4/FLB-full | Favorite-space calibration, FULL 15m history (13,765 mkts, snapshots, cluster CI) | train | 0.6-0.7 +1.04pp [0.06,1.92]; 0.8-0.9 +1.55pp [0.72,2.24]; 0.9-0.97 +0.54 [0.07,1.08]; 0.97-1 +0.41 [0.24,0.58]; both sides positive | CONFIRMED unconditionally |
| H5 | FLB by phase | train | 0.8-0.9 EARLY +2.09pp [0.93,3.18] vs LATE +0.41 [-1.08,1.85] | REVERSED - bias lives early-window, not late |
| H15 | Estimator race: N(d2) vs market mid, 613K pts, 6,621 mkts | train | market 0.13147 beats ALL estimators (best 0.13422); market wins EVERY phase; crushes final (0.0416 vs 0.0488) | MARKET WINS - paper's premise dead at scale |
| H16 | t4-tails and seasonality variants | train | both worse than calibrated Gaussian in Brier | NO HELP in mid-band; tails matter only at extremes (G(z) handles) |
| H1 | Paper rule (N(d2)+2pt buffer, taker) on 6,619 mkts | train | ewma5m: -1.68c/sh [-2.85,-0.46]; ewma30m: -1.22 [-2.45,-0.01]; all taker nulls negative | CONFIRMED NEGATIVE - published strategy loses money at scale under real fees |
| Edge-Brier | G(z) w/ Binance-adj spot vs market | train | market wins every phase (final 0.0415 vs 0.0484) | market is Binance-informed and fast |
| Comb | logit(mid)+z combination forecaster | internal val | 0.13240 vs mid 0.13246 - trivial gain; z weight big only early-window | MARKET ~EFFICIENT in mean; exploit level shifts, not forecasts |
| SBT-gz | GzValueMaker backtest (maker at bid on model divergence, q=400) | train | -2.59c/sh, -6.12% stake, win 39.7% | STRONG NEGATIVE - adverse selection vs better-informed flow |
| SBT-flb1 | FLB maker 0.78-0.92, tau<=66%, veto, q=400 | train | -0.77c/sh [-5.2,+1.1]/mkt; fill-cond win 83.1% vs uncond 85.5% | NEGATIVE - adverse selection 2.4pp > bias 1.55pp |
| PRINTS | Fill-conditioned maker alpha map, phase x price (all printed volume) | train | POCKETS: early favs 0.7-0.97 +1.6..2.3c ($24M vol); mid favs 0.8-0.97 +0.9..1.5c; ENDGAME longshots 0.03-0.3 +5..15c ($12M); belly & final favs negative | MAP FOUND - pockets to validate with cluster CIs |
| ENDGAME-15m | Endgame longshot pocket forensics (maker bids 0.03-0.30, last 13.5s) | 136K prints, 3,150 mkts, 147 days, $3.5M vol | +12.2c/sh; day-cluster CI [5.4,19.6]; 48% days negative; top-5 days = 70% of alpha; win 24.6% at entry 0.124 (~98% EV/stake) | REAL but lottery-shaped; jump-catcher |
| SBT-ef-q400 | Early-fav maker pocket backtest, back-of-queue | train | -0.77c/sh [-5.0,+1.5]/mkt | queue position decides who gets the pocket |
| FLB-hourly | Hourly family favorite-space FLB (3,421 mkts) | train | 0.8-0.9 +1.92pp [0.19,3.48]; Down-favs +2.0-2.6pp; larger than 15m | H26 SUPPORTED - less bot attention, bigger bias |
| MAP-hourly | Hourly maker alpha map | train | replicates all 15m pockets (early favs +1.4-1.9c, mid favs +1.9c, endgame longshots +9.1c, endgame favs negative) | CROSS-FAMILY REPLICATION |
| SBT-eg-join | Endgame join-the-touch longshot bid | train | -4.2 to -4.8c/sh; stale-tape chasing | NEGATIVE - wrong implementation of pocket |
| SBT-ladder | Fixed deep-bid ladders 0.05-0.30 (last 90s / last 13.5s) | train | -6.7 / -6.5 c/sh; only 0.05 level positive | NEGATIVE - fixed levels sell favorites below fair |
| DECOMP | Endgame pocket decomposition by mirror/aggressor | train | dumps into bids +3.9c (n=11K); favorite-chase passive side +14.8c (n=53K, fails 27.9% at 0.869!) | POCKET = SELLING THE CHASED LEADER ABOVE FAIR, not deep bids |
| SBT-eg-offer | Sell leader at fv+margin, endgame | train | -1.0 to -1.8c/sh; offers landed at 0.95+ where takers are informed | NEGATIVE - wrong region |
| GAP-DECOMP | Endgame favorite-buy prints by (price - model fair) | train | ALL alpha in gap>0.4: $1.03M vol, price 0.835 vs fv 0.068, fail 84.1%, maker +67.5c/sh = winning-side panic dumps into book vacuum; gap<-0.05 is poison (-20c) | THE POCKET = deep bids on the MODEL-WINNING side |
| VAC-15m | Model-gated vacuum ladder (bids 0.10/0.20/0.30 on fv>=0.65 side, last 27s, q=200) | train 72d | +39.2c/sh, +190% on stake; win 58-62% at 10-30c levels; day-CI [$338,$1902]/day at 300sh/mkt sizing; top-5 days 86% | STRONG POSITIVE - to validate: 5m/hourly replication, test period, capacity |
| VAC-cap | Vacuum ladder capacity (SIZE=1000/level) | train | +38.1c/sh unchanged; $9.5K/day, day-CI [$2.6K,$17.7K]; stake $368K/72d | CAPACITY ~10x base, no alpha decay |
| VAC-queue | QUEUE=1000 vs 200 | train | identical results | queue-position IRRELEVANT - sweep-through fills; no speed race |
| VAC-gate | FV_GATE=0.80 | train | +46.1c/sh, same $/day | tighter gate = purer, robust to gate choice |
| VAC-hourly | Vacuum ladder on hourly family (both proportional and absolute-27s timing) | train | -2.7 to -5.6c/sh; only 0.10 level positive; 259-329 fills (10-20x thinner flow); day-CI includes zero | NO DETECTABLE EDGE on hourly - thin dump flow; 5m replication decisive |
| VAC-5m | Vacuum ladder 5m family, last 13.5s (POST_FRAC=0.955), q=200, size=100 | train 74d | +28.5c/sh, +138% stake; 8,388 fills/3,026 mkts (4x 15m); win 46/49/52% at 10/20/30c; day-CI [$1157,$5645]/day, mean $3202/day; top-5 days 79% | REPLICATED - independent family, larger scale |
| VAC-TEST | Frozen-param TEST reveals (Jun15-Aug5, 51d) | test | 15m: -9.15c/sh, day-CI [-129,-63]; 5m: -5.71c/sh, day-CI [-510,-272]; win rates collapsed to 4-23% | FAILED OUT-OF-SAMPLE |
| VAC-WEEKLY | Weekly P&L profile, 5m, full span | all | ALL profit from Apr20-May10 crash episode (+$213K of +$237K in one week, win 85%); EVERY other week negative (-$0.3-3.8K/wk, win 11-22%) incl. early April (in-train) | RECLASSIFIED: crash-event harvester, n=1 event; NOT a continuing edge; bleeds in normal regimes |
| FASTCANCEL | Cancel-on-move maker defense, mid-window favs, latency 0.5s/2s/10s/inf | Jun-Aug | alpha -0.74/-0.63/-0.89/-0.81 c/sh - latency-invariant | NEGATIVE - informed-at-crossing flow; speed does not rescue makers |
| VAC-GATE | Dump-activity gating of vacuum ladder | full span | normal-week bleed unchanged (-$30K); no discriminating power | NEGATIVE - bursts mimic crashes |
| ABOVE-VOL | Above-strike hourly family total volume | full | 163,447 trades TOTAL (~4.6/mkt) | family is illiquid; H12/H14/H29 capacity ~ zero |
| H12 | Ladder monotonicity scan | full | 90 apparent violations / 64K snapshots, mean 12.6c gross | stale-print artifacts on illiquid family; NOT executable; CLOSED |
| H27 | Daily above family | - | untested in depth; family thinner than hourly above | CLOSED as untradeable at scale |
| H1-5m | Paper rule on 5m family | train, 19,865 trades | -2.28c/sh [CI -2.97,-1.63] | CONFIRMS: loses harder on 5m |
| H15-5m | Estimator race 5m | 1.39M pts | market beats G/Phi every phase (final 0.0444 vs 0.0574) | CONFIRMS market sharpness |
| FLB-above | Above family FLB | 25 usable snapshot-mkts | n too small; family illiquid | CLOSED |

## Round 2 — "think outside the box" pass (2026-08-09)

| # | Hypothesis / test | Data | Result | Status |
|---|---|---|---|---|
| H31 | PRE-WINDOW mispricing: for updown, strike=spot at t0, so fair should be exactly 0.50 before open. Never examined — all prior grids started at tau>=0 | 6.0M pre-window prints, $130M notional, 68K mkts | Identity is FALSE empirically: P(Up) tracks traded price almost exactly (px 0.44->43.3%, 0.53->53.3%, 0.61->63.2%; gap ~0 in every bucket). Pre-window market is calibrated | REJECTED — venue efficient pre-open |
| H31b | Pre-window under-reaction (gaps +/-8pp at the extremes) traded as taker | train+test both families | -1.0 to -2.9c/sh; fee+spread eats it | NEGATIVE |
| H31c | Same as maker on favored side, causal signal, real fill sim | 322K fills train / 66K test | 5m +0.72c train/+2.90c test; 15m +2.04 train/-2.36 test; ALL CIs span zero | NOT VALIDATED |
| H32 | Timestamp artifact check on pre-window prints | receipt clock vs exchange clock | lag 0.0s, 0.000% inside window — prints are genuinely pre-open | CLEAN (no artifact) |
| H33 | WINDOW-OPEN structural moneyness: oracle lags Binance ~2-3.5s, so the strike is a stale price and the window opens already ITM by a knowable amount | 34K (5m) + 11K (15m) mkts | Mean model-vs-market gap at open = 4.0c. Assumed-fill maker: +1.60c test 5m CI[+0.59,+3.11], +3.18c test 15m CI[+0.58,+5.18] — CIs exclude zero | PROMISING until fills simulated |
| H33b | Same with REAL fill simulation (print must sweep our level) + anti-side null | same | Collapses to -1.99 to -3.28c/sh; fill rate 85% (you fill exactly when price runs through you). Model side beats anti side consistently (model has information) but both lose | NEGATIVE — adverse selection, same as every other maker test |
| H34 | Maker rebate economics (never credited before) | live venue config | `feeSchedule.rebateRate = 0.2` -> maker earns 0.2*0.07*p(1-p) = max 0.35c/sh. `rewardsMaxSpread=1.5, rewardsMinSize=50` exist but NO clobRewards pool attached to these markets | REAL BUT SMALL — 0.35c cannot offset 2-3c adverse selection |
| H35 | Endgame taker in the near-zero-fee zone (ask>=0.90, fee ~0.5c), 300ms latency penalty | Jun-Aug, 8.1M quote obs | 5m test +0.67c/sh CI[-0.98,+2.12]; 15m test +0.07c CI spans 0. **Median available size = 9-10 shares** | CAPACITY-DEAD (~$2/day) |
| **H36** | **SETTLEMENT RULE CHANGE**: on 2026-08-07 all 384 markets/day switched description from "price at the end of the range" to "time-weighted average price (TWAP) of the range" | markets dataset, 100% sharp changeover | **Wording change CONFIRMED.** Outcome data (Binance proxy): END-rule still correct 79.2% on rule-disagreement markets (n=106) vs 87.6% pre-baseline -> does NOT support a math change. BUT full-sample END match fell 0.954 -> 0.897 (n=766, ~5 sigma) | **OPEN — something changed, not identified** |
| H36b | Prize if settlement IS average-based | real BTC paths, 23K windows | At 95% through a window: correct (TWAP) model Brier **0.0021** vs terminal model 0.108 vs market's historical final-phase 0.042. |Δ|>25pp in 19% of cases | QUANTIFIED — large if H36 resolves as TWAP |
| **H36-RESOLVED** | Settlement rule change identified (user-supplied, then verified) | Aug 2-9, on-chain results | **NEW RULE: Up iff mean(P,[T-w,T]) >= mean(P,[0,w]), w=30s for 5m / 60s for 15m; live 2026-08-07; all coins BTC/ETH/SOL/XRP/DOGE.** Head-to-head on markets where old and new rules disagree: OLD correct **80.5%** PRE (n=200) -> **49.1%** POST (n=55). The old terminal rule stopped governing exactly at the changeover | **CONFIRMED** |
| H37 | Correct pricer vs legacy pricer under the new rule, scored on true outcomes | 26.2K 5m + 8.7K 15m real windows | 5m at 5s left: Brier **0.0026** (correct) vs **0.0449** (legacy) = 17x; 15m at 10s left 0.0017 vs 0.0211 = 12x. p90 disagreement 22pp; **12% of windows disagree >15pp**. Market's historical final-phase Brier was 0.042 | **QUANTIFIED — largest edge found in the project** |
| H37b | Implication for prior findings | — | Vacuum ladder is now structurally dead (it harvested last-second spikes, which a 30s/60s average neutralises) — DISABLED in bot. All incumbent models (paper's N(d2), our G(z), any pre-Aug-7 bot) price the wrong contract | — |
| H37c | Has the market adapted? | **UNKNOWN — no post-change price data** | tapes end Aug-4, Telonex quota exhausted. RollAvgEdge strategy now logs correct-vs-market gap every tick in the final 45s so paper mode answers it directly | **OPEN — measuring now** |

## Round 3 — AHL multi-horizon momentum adapted to 5m binaries (2026-08-09)

| # | Test | Data | Result | Status |
|---|---|---|---|---|
| H38 | AHL multi-horizon score (sign of P_t - P_t-k, k=5/10/21/42 bars) vs next 5m binary outcome | 88,619 windows, Oct25-Aug26 | **Signal REAL but INVERTED**: score=+4 -> P(Up)=0.461, score=-4 -> P(Up)=0.525. Spread -4.2pp, **t=-11.25**. BTC 5m MEAN-REVERTS after multi-horizon trends (AHL momentum works at daily+ horizons; 5m is the short-term-reversal regime) | **STRONG SIGNAL** |
| H38b | Is the reversal universal? | ETH/SOL/XRP/DOGE/BTC, 17,483 windows each, Jun-Jul | ALL five coins: spread -6.0 to -8.3pp, t=-5.1 to -7.1. **ETH strongest (-8.3pp), SOL second (-7.2pp), BTC only 4th** | **UNIVERSAL** |
| H38c | Does the Polymarket book already price it? | 34,337 BTC windows w/ quote at open | **YES.** Book mid at open = 0.517 when score=-4, 0.483 when score=+4 — the book already fades the trend, in the right direction and near the right size | KEY FINDING |
| H38d | Residual: is the score incremental to the book? | train/test | Book UNDER-fades by ~1.1-1.4pp, same sign train AND test (pooled t~2.3 train / 1.7 test). But **1.2pp < 1.75c taker fee** | REAL BUT SUB-FEE |
| H38e | Executable: fade-the-trend at the open | 13,503 mkts, |score|=4 | TAKER -0.5 to -1.9c (fee exceeds edge). MAKER assumed-fill +1.1 to +2.4c, but **REAL fill sim: -2.06c train / -3.20c test** at 84% fill rate | NEGATIVE — 4th family to hit the same adverse-selection wall |
| H38f | Trade the signal on the UNDERLYING instead (true AHL form) | 88,618 5m bars | **Mean next-5m return ~0** (-0.05 to -0.21 bps, t=-0.4/-1.9) even though the SIGN shifts 3.9pp. Signal moves the median, not the mean. Turnover 0.4/bar -> -390% ann. at 1bp cost | NEGATIVE — and explains why a binary is the RIGHT instrument for a sign signal |
| **H39** | **OPEN LEAD**: the reversal is universal and STRONGER on ETH/SOL, but only BTC's book was tested for whether it prices it. If a thinner coin's book fades less than BTC's while its true reversal is 38% larger, the residual could exceed the 1.75c fee and be taker-viable | needs ETH/SOL tapes (Telonex quota exhausted) | Threshold to clear: residual (actual - mid) > ~1.75c at mid prices. BTC's residual was 1.2c | **UNTESTED — highest-value next test** |

## Round 4 — free public data (no Telonex quota) and the post-2026-08-07 contract

Unlock: three unauthenticated Polymarket endpoints carry everything needed —
gamma `/markets?slug=..&closed=true` (100 slugs/req, outcomes + resolutionSource),
`clob/prices-history`, and `data-api/trades` (the full trade tape WITH wallet ids).
229,530 resolved 5m markets across btc/eth/sol/xrp/doge, 2026-03-01..08-09.

| # | Hypothesis | Result | Verdict |
|---|---|---|---|
| H40 | Settlement change datable from venue metadata | `resolutionSource` flips `<coin>-usd` -> `<coin>-usd-twap-30s-streams` on exactly 2026-08-07, all 5 coins simultaneously | CONFIRMED |
| H41 | AHL reversal is universal and strongest on ETH | spread P(Up\|score<0)-P(Up\|score>0): ETH +7.05pp (t=13.6, train 7.06 / test 7.06), SOL +4.95, DOGE +4.34, XRP +3.48, BTC +3.34. n=229K | CONFIRMED (signal) |
| H39 | Alt books fail to price the reversal | Books already fade it at open (bid/ask 0.51/0.53 at score -4 vs 0.47/0.49 at +4). Taker EV ~0, CIs span 0 | REFUTED |
| H42 | Wallet skill persists out of sample | Spearman +0.43; top decile +4.44c/sh in test vs bottom -0.76c | CONFIRMED |
| H43 | Informed wallets are copyable | -6.77c/share at 1s lag; their edge is price selection, not direction | REFUTED |
| H44 | Venue-wide maker alpha exists | 66.6M shares: taker gross +0.09c, net -0.76c => maker gross **-0.09c**. Cell map train/test corr **-0.35** | REFUTED |
| H45 | The new rule is trailing/trailing, not forward-strike | Accuracy post-change: trailing/trailing **0.9503** vs old 0.8904 vs forward-strike 0.8943. Window scan peaks exactly at w=30s. On 266 disagreeing markets trailing-TWAP right **82.7%** vs old 17.3% | **CONFIRMED — corrects my own earlier assumption** |
| H46 | The book has NOT re-priced the new contract | Endgame taker EV vs real prints, last 60s, \|z\|>=2: **+3.04c/share** (per-day SE 0.07, 3 days, 2.56M shares). Pre-change control on identical machinery: **+0.19c**. All 15 coin x day cells positive | **FOUND — pending depth + live confirmation** |

Settlement rule (verified): `Up iff mean(P over [t1-30, t1)) >= mean(P over [t0-30, t0))`.
The strike is BACKWARD-looking and known at t0.

## Round 5 — deployment, live audit, and gate 1 (2026-08-10)

| # | Hypothesis / check | Result | Verdict |
|---|---|---|---|
| H47 | The RTDS feed publishes an already-TWAPed price (would make our averaging a double-average) | Lag-1 autocorr of its 1s returns +0.24 (a 30s TWAP would be ~+0.9); sd ratio vs Binance 1.23, not 0.18 | REFUTED — feed is spot rounds, averaging is correct |
| H48 | Phi(z) is an adequate fair value | z>3 settles Up **91.6%**, not Phi(3)=99.87%. Using Phi made `fair-ask>=edge_min` vacuous | REFUTED — replaced by empirical `bot/calib.py` |
| H49 | The empirical-fair edge filter improves the strategy | It CHANGES it: fires on 5x fewer shares at avg px **0.389**, hit 0.501, +10.15c/share, but pre-change control **+2.24c** vs +0.49c. A longshot effect, not the settlement change | SEPARATE EFFECT — behind `require_edge`, off by default |
| H50 | The endgame mispricing is available as resting depth (GATE 1) | Flow IS takeable: 85% of settle-window volume is BUY prints, **67% at or below the standing best ask**. BUT at \|z\|>=2 the favoured side rests at median **0.990**; only 6% of snapshots have >=10 shares at <=0.97; 41% of the time best ask >0.97. 1s polling filled **1 market in 17**, 21 of 100 shares | **NOT PASSED — open.** Edge is in transient dips; bot made event-driven |

Live-audit bug tally: 12. The five that would each have lost money silently:
paper-broker settling on the pre-change rule; oracle tick-sum instead of a
time integral; duplicate bot instances sharing the fill log; a cold EWMA
reading 22x low; and the vol estimator not being the backtest's estimator
(5x low). Two more would have wasted the 48h gate-1 wait: `depth_sim`
reading files the pruner deletes, and testing a 60s window with Phi(z)
instead of the bot's 30s settle window with the empirical fair.

| # | Hypothesis | Result | Verdict |
|---|---|---|---|
| H51 | If the taker path is depth-constrained, rest a bid to CATCH the dips instead | Fills 11-14% of markets but at hit rate **0.676** vs the taker's 0.916 — you are filled precisely when the favoured side is collapsing. **-26.4c/share** (day-SE 3.9, t=-6.8) at fair-0.02, and **-25.1c pre-change** on the same machinery. Queue-irrelevant (queue 0 vs 200 differ 0.3c) | **REFUTED** — pure adverse selection, unrelated to the rule change |

Consequence: the taker path is the ONLY viable execution for this edge, so
gate 1 (ex-ante resting depth at <=0.97) alone decides whether it is
tradeable. There is no maker fallback.

| # | Hypothesis | Result | Verdict |
|---|---|---|---|
| H52 | The 15m family (which the bot also trades, 44% of live paper shares) carries the same edge | post-change \|z\|>=2 ask<=0.97: **+6.18c/share**, hit 0.781, 52,916 shares, 162 markets. Pre-change control **-3.26c**. Contrast is SHARPER than 5m (whose control is +1.39c). w=60s confirmed by resolutionSource `*-twap-60s-streams` and an accuracy peak at exactly 60s (0.9744) | **CONFIRMED** — `src/validate_15m.py` |

Gap this closed: the headline measurement globbed `*_5m_*` only, so the bot
was trading an unvalidated family until this ran.

| # | Hypothesis | Result | Verdict |
|---|---|---|---|
| H53 | max_price=0.97 is leaving the reachable edge on the table | The 0.985-0.990 band holds **1.22M of 2.6M** qualifying shares at **+0.57c/share** (hit 0.996, fee only 0.07c) vs **-0.64c** pre-change. Raising the cap 0.97->0.99: shares/day 353K->889K, markets/day 367->862, pool $26.0K->$29.3K, blended EV 7.37c->3.30c | **CONFIRMED — cap raised.** It was the binding constraint on px_pass (2.3% live), and it was self-imposed |

Caution recorded: at ~0.9875 the payoff is ~79:1 against (lose 98.75c, win 1.25c).
EV survives a hit rate down to ~0.990 and turns negative below ~0.985, so the
margin for model error is ~1pp of hit rate. The per-market DOLLAR cap, not the
share cap, is what bounds the tail; set to $150.
