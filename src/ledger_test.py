"""The daily stop must read the venue, and must not fire on a bad read.

day_pnl only counts markets that settled while the process was alive,
so a restart across a settlement loses that P&L for good -- live it read
+0.28 against a real -12.15. Since day_pnl is what the daily loss limit
reads, the stop was blind by the full drift.

Guards the three ways the fix could itself be wrong: counting an open
position as a loss (which would trip the stop on every held market),
failing to fire once the venue figure breaches the limit, and treating
an unreadable feed as a flat day.

    python3 src/ledger_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot import ledger
from bot.risk import Risk

# --- settle_ts across families ---------------------------------------
assert ledger.settle_ts("btc-updown-5m-1787409900") == 1787410200
assert ledger.settle_ts("btc-updown-15m-1787409900") == 1787410800
assert ledger.settle_ts("garbage") is None
print("settle_ts ok")

# --- open markets are NOT losses --------------------------------------
now = 1787410000.0
rows = [
    # settled market, bought 12.70, nothing back -> a real -12.70
    {"slug": "btc-updown-5m-1787409000", "type": "TRADE", "side": "BUY",
     "usdcSize": 12.70, "size": 71, "price": 0.17, "timestamp": 1787409100},
    # market still running: money out, nothing back YET -- not a loss
    {"slug": "btc-updown-5m-1787409900", "type": "TRADE", "side": "BUY",
     "usdcSize": 8.00, "size": 8, "price": 0.99, "timestamp": 1787409950},
]
by = ledger.by_market(rows)
net = 0.0; openn = 0
for slug, d in by.items():
    t1 = ledger.settle_ts(slug)
    if now < t1 + ledger.GRACE_S:
        openn += 1; continue
    net += d["sell"] + d["redeem"] - d["buy"]
assert abs(net + 12.70) < 1e-9, net
assert openn == 1, openn
print(f"settled net {net:+.2f}, {openn} open position excluded")

# --- the stop fires on the venue figure, not the accumulator ----------
r = Risk({"daily_loss_limit": 20.0})
r.day_pnl = 0.28                     # what the bot thought
assert not r.killed
drift = r.reconcile_day_pnl(-12.15)  # what the venue says
print(f"drift corrected {drift:+.2f} -> day_pnl {r.day_pnl:+.2f} "
      f"killed={r.killed}")
assert abs(drift + 12.43) < 1e-9 and not r.killed

r2 = Risk({"daily_loss_limit": 20.0})
r2.day_pnl = 0.28
r2.reconcile_day_pnl(-25.0)
assert r2.killed, "stop must fire once the venue figure breaches the limit"
print("stop fires at -25.00 against a 20.00 limit")

# --- an unreadable feed must not read as a flat day -------------------
assert ledger.realised_since("", 0) is None
print("empty wallet -> None (never 0.0)")
print("\nPASS: the stop reads the venue and refuses to guess")
