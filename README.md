# Polymarket Short-Dated BTC Binaries — Research & Trading Bot

Research program built on top of "Fair Value Pricing in Short-Dated Bitcoin
Binary Markets" (Semenas, 2026), taken from a 182-trade single-window study
to a ~120,000-market, five-family, ten-month evaluation — and from there to
a deployable strategy the paper did not find.

## TL;DR

- The paper's strategy (N(d2) divergence, taker) **loses** money at scale
  under real fees: -1.7c/share on 6,619 markets.
- The market's own mid **beats every standard model** as a forecaster; this
  venue's inefficiency is not informational. It is behavioral, and it is
  concentrated in specific (phase x price x flow) pockets.
- The surviving, validated strategy is the **vacuum ladder**: resting deep
  bids on the model-favored side in the final ~27 seconds, which get filled
  only when panicking holders dump *winning* positions into empty books.
  Train: +39c/share, ~$1.0K/day at $60-per-market risk (day-clustered CI
  [$338, $1,902]), ~10x capacity headroom. See reports/ for full evidence
  and audit.

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
