# Polymarket BTC binaries bot

Paper-first trading bot for `btc-updown-{5m,15m}` markets. Prices contracts
with the empirical G(z) curve (fit from the Chainlink settlement feed +
Binance vol; see `src/fit_gz.py`), using basis-adjusted Binance spot and the
captured oracle strike per window.

## Components
- `recorder.py` — standalone live data recorder (Binance trades, RTDS
  oracle ticks, CLOB books) -> `data/live/*/`. Run it always; every hour
  recorded is out-of-sample validation data.
- `state.py` — online state: books, oracle, Binance, EWMA vol, basis
  ratio, strike capture, G(z) pricer.
- `strategy.py` — signal generators (`GzValueMaker`, `ExtremeTaker`);
  parameters in `config.json`.
- `paper.py` — paper broker with backtest-identical conservative fill
  rules (prints through level = fill; at level = fill beyond queue).
- `risk.py` — staleness refusal, position caps, daily loss kill switch.
- `run.py` — main loop (paper mode).

## Run
```bash
cd /home/user/JSF
python3 bot/recorder.py &          # data recording (always on)
python3 bot/run.py                 # paper trading
```

Logs: `logs/decisions.jsonl` (every signal and sizing decision),
`logs/paper_fills.jsonl` (orders, fills, settlements).

## What to watch
- `settle` records in paper_fills: per-market P&L.
- `feed_err` records in decisions: feed health.
- Kill switch: `risk.killed` trips at the daily loss limit; restart
  after review only.

## Live mode
Not enabled. Requires: paper-vs-backtest reconciliation pass, then a CLOB
client (py-clob-client) with API creds wired into the `PaperBroker`
interface (same method signatures), and fee verification on first fills.
