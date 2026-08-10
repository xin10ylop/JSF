# Polymarket Short-Dated BTC Binaries — Research & Trading Bot

Research program built on top of "Fair Value Pricing in Short-Dated Bitcoin
Binary Markets" (Semenas, 2026), taken from a 182-trade single-window study
to a ~120,000-market, five-family, ten-month evaluation — and from there to
a deployable strategy the paper did not find.

## TL;DR

- The paper's strategy (N(d2) divergence, taker) **loses** money at scale
  under real fees: -1.7c/share on 6,619 markets. Its premise is inverted —
  the market's own mid beats every standard model as a forecaster.
- On the **old** contract nothing worked. Measured across 66.6M shares of
  public tape, takers earn +0.09c/share gross and lose exactly the fee;
  makers therefore earn **-0.09c/share** gross. There is no always-on edge
  there, and the (price x phase) maker map has train/test corr **-0.35**.
- On **2026-08-07 Polymarket changed the contract** and the book has not
  caught up. Settlement moved to a *trailing* TWAP at both boundaries:

      Up iff mean(P over [t1-w, t1)) >= mean(P over [t0-w, t0))
      w = 30s (5m markets) / 60s (15m)

  verified from venue metadata (`resolutionSource` -> `*-twap-30s-streams`)
  and from 2,880 settled outcomes (0.9503 accuracy vs 0.8904 for the old
  rule; 82.7% correct on the 266 markets where the two rules disagree).
- Inside the last w seconds the outcome is progressively **locked** —
  remaining variance decays as `rem^3`, not `rem`. A book still quoting the
  old terminal contract is wrong there in a computable direction.
- **The strategy**: take the favoured side inside the settle window when
  `|z| >= 2`. Against real prints, net of the verified fee:
  **+3.04c/share (per-day SE 0.07)** on 2.56M shares, five coins — versus
  **+0.49c** for the identical machinery on a pre-change control. All 15
  coin x day cells positive; survives a **3x** stress of the vol estimate.
- Capacity ~88K shares/day at a 100-share/market cap ($2,640/day of edge in
  the pool) on ~$230 of working capital. Plan against a 10-30% capture.
- **This is a re-pricing lag and lags close.** ETH decayed +5.77 -> +4.96 ->
  +1.41c over three days; BTC has held. The bot logs the gap every tick so
  decay is measured, not assumed. See `reports/findings.md` §6 for risks.

## Repository map

- `reports/venue_facts.md` — verified venue mechanics (families, settlement
  conventions with match rates, fees, microstructure, latency structure)
- `reports/hypotheses.md` — the 30-hypothesis register
- `reports/results_table.md` — every test run, wins and losses
- `reports/findings.md` — the narrative: what was reproduced, refuted,
  and found
- `reports/audit.md` — strategy & bot audit (backtest-vs-live parity)
- `src/` — data pipeline + research code (see RECOVERY.md for rebuild)
- `bot/` — recorder + paper-trading bot (see bot/README.md)
- `deploy/droplet_setup.sh` — one-shot droplet deployment (systemd)
- `supervisor.sh` — idempotent research-pipeline runner

## Quick start (droplet)

```bash
REPO_URL=https://github.com/xin10ylop/jsf.git bash deploy/droplet_setup.sh
journalctl -u jsf-paperbot -f
```

Paper mode only. Live order submission is intentionally not wired until the
paper-vs-backtest reconciliation gates in reports/audit.md all pass.
