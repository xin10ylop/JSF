# Findings: Polymarket Short-Dated BTC Binaries

Final report (data pipeline coverage: Oct 2025 - Aug 2026; five BTC
families; ~120K resolved markets; Chainlink settlement feed at 1s; Binance
at 1s/100ms; 3,000-market book sample; live recorder + paper bot running).
Discipline: train < 2026-06-15 < test; test revealed once with frozen
parameters. Everything measured net of the verified current fee schedule.

## 1. Verdict on the paper

The paper claimed a +4.6pp edge for a driftless N(d2) divergence rule on
182 trades in one two-day window, with candid caveats. At scale all of its
caveats resolve against it:

- **Its strategy loses money**: taker at touch with a 2pt buffer nets
  **-1.68c/share [CI -2.85,-0.46]** on 6,619 markets. Signals cluster near
  p=0.5, exactly where the 0.07*p*(1-p) taker fee peaks. (H1)
- **Its premise is inverted**: the market mid beats N(d2) under every vol
  estimator tested (9 estimators x Gaussian/t/seasonal variants), in every
  window phase, most decisively near expiry (Brier 0.0416 vs 0.0488). A
  logit(mid)+z combination beats mid by a meaningless 6e-5. (H15, H3)
- **What it got right**: the driftless reduction (momentum adds nothing -
  H18), the framing of these contracts as digitals, and the honesty of its
  own caveats. Its "open question" - real edge vs favorite-longshot bias -
  resolves: the in-window favorite premium is real (+1.0-1.9pp,
  direction-robust, strongest early-window) but is NOT harvestable by any
  execution its framework implies.
- **Beyond its lens**: Gaussian tails are wrong at these horizons
  (t~5; P(|z|>3) is 10x Gaussian; a "2-sigma-certain" side with <30s left
  is worth 0.91, not 0.977). We replaced N(d2) with an empirical isotonic
  G(z) fit on ~800K settlement observations. Even that does not out-forecast
  the market - but it correctly prices tails where the crowd's chasing is
  worst.

## 2. The venue's structure (what the data shows)

- **Settlement verified**: updown = first Chainlink round at/after each
  boundary (99.996% match, 47,590 mkts); hourly families = Binance candles
  (100%). Ties go Up.
- **Information frontier**: Binance leads the oracle round by ~2s + 1.1s
  broadcast lag. The venue's top of book already embeds it - the fee
  regime killed classic latency sniping and protects the book.
- **Microstructure**: 1c spreads, $100-300 depth near the touch,
  event-driven books, mirrored sibling books with mint/merge crossing.
- **Behavioral regularities** (all cluster-robust): favorites underpriced
  1-2pp (both sides, strongest early-window, larger on hourly); longshots
  overpriced; final-second "leader chasing" and "winner dumping" flows.

## 3. Strategy search: what was tried and what happened

| Strategy family | Result |
|---|---|
| N(d2)/G(z) divergence, taker | -1.7c/sh - fees + market sharper than model |
| G(z) divergence, maker | -2.6c/sh - adverse selection (fills = model wrong) |
| FLB harvest, maker join (all phases / early-only / front-queue) | -0.8 / -0.8 / +0.3c [CI spans 0] - adverse selection eats the bias |
| Fast-cancel maker (0.5s-10s cancel-on-move) | latency-INVARIANT -0.6..-0.9c - crossing flow is informed at the instant it crosses |
| Endgame join-the-touch / fixed deep ladders / leader-offer | -1.0 to -6.7c - each intercepts the toxic slice of endgame flow |
| **Vacuum ladder** (deep bids on model-winning side, last 27s) | Train: +39c/sh (15m), +29c/sh (5m), day-CI positive, 10x capacity, queue-irrelevant. **Test: -9.2c and -5.7c/sh, CIs firmly negative.** Weekly profile: ALL profit from the Apr 20 - May 10 crash episode; every other week bleeds. n=1 payoff event. |
| Dump-activity gating of the above | No discriminating power - informed bursts mimic panic bursts |
| Riskless structures (crossed books; strike-ladder monotonicity) | Crossed books: median 1c x 24 shares - scraps. Ladder arbs: see results table (H12) |

## 4. The honest conclusion

**This venue, in its current regime, does not offer a validated always-on
edge to a new entrant at any execution style we could test.** The crowd's
mid is sharp; taker costs are prohibitive by design; and the passive side
is adversely selected at every speed - the behavioral premia measurably
exist but accrue only to counterparties of uninformed crossings, which no
observable ex-ante filter isolates.

What genuinely exists:
1. **The crash-harvest (vacuum ladder)**: during the one violent BTC
   dislocation in our sample (late Apr - early May 2026), panic dumping of
   winning positions into empty books paid resting deep bids ~+65c/share
   for two weeks (~$250K at $60-per-market sizing across 5m+15m). Between
   such episodes the same ladders bleed ~$1.5-4K/week at that sizing, and
   the June-Aug regime shows the residual flow is now informed (possibly
   competitors). This is a speculative event strategy with a single
   observed payoff - deployable only at sizes whose bleed you accept as an
   option premium on the next crash, and only with live paper evidence
   that the current-regime bleed matches expectations.
2. **A real-time measurement apparatus**: the paper bot + recorder
   (deployed) measure the venue's regime continuously - fill rates, dump
   frequency, bleed - so a returning crash regime is detected from data,
   not hope.

## 5. Relative to the paper

Reproduced: the framework, the driftless reduction, the direction-robust
favorite premium it could not disentangle. Refuted: its strategy's
profitability under real fees at scale; its premise that a spot+vol model
out-forecasts this market. Found beyond it: the correct (fat-tailed,
empirical) pricing curve; the verified settlement/latency microstructure;
the complete fill-conditioned map of where maker money actually goes; and
the crash-flow phenomenon - the one structural inefficiency large enough
to matter, together with the evidence discipline showing exactly how far
it can currently be trusted.
