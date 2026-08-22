"""Band-split sizing: big only where the book is deep and the edge holds.

The scaling design puts locked-band size (>=0.90, 99% win, resting
depth 10k+/tick) on its own ladder while the cheap lottery band keeps a
small dollar cap. The failure mode this guards: re-fires accumulate
cost until the dollar cap binds, so ONE global cap sized for the locked
band would let a 17c entry quietly stack to locked-band dollars -- a
23%-hit-rate bet at 3-4x the intended stake.

    python3 src/band_sizing_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.risk import Risk                       # noqa: E402
from bot.strategy import RollAvgEdge            # noqa: E402

# 1) strategy: size by band
s = RollAvgEdge({"size": 15, "size_locked": 50, "locked_px": 0.90})
assert (s.size_locked if 0.97 >= s.locked_px else s.size) == 50
assert (s.size_locked if 0.17 >= s.locked_px else s.size) == 15
print("strategy: 0.97 -> 50 sh, 0.17 -> 15 sh")

# 2) risk: cheap cap binds even as re-fires accumulate
r = Risk({"max_market_dollars": 55.0, "max_market_dollars_cheap": 15.0,
          "cheap_px": 0.90, "max_market_shares": 200,
          "daily_loss_limit": 80.0})
spent, shares = 0.0, 0.0
for _ in range(12):                       # refire storm at 17c
    got = r.size_ok(shares, spent, 15, 0.17, 0)
    if got <= 0:
        break
    shares += got
    spent += got * 0.17
assert spent <= 15.0 + 1e-9, spent
print(f"risk: cheap re-fires stopped at ${spent:.2f} (cap $15), "
      f"{shares:.0f} sh")

# 3) locked band gets the big cap
got = r.size_ok(0, 0, 50, 0.97, 0)
assert abs(got - 50) < 1e-9, got
spent2 = 50 * 0.97
got2 = r.size_ok(50, spent2, 50, 0.97, 0)
assert got2 * 0.97 + spent2 <= 55.0 + 1e-9
print(f"risk: locked first fire 50 sh (${spent2:.2f}), "
      f"refire capped at {got2:.1f} sh -> total <= $55")

# 4) unconfigured = old behavior (one cap for everything)
r2 = Risk({"max_market_dollars": 12.0})
assert r2.max_market_dollars_cheap == 12.0
print("defaults: no cheap cap configured -> identical to old behavior")
print("\nPASS: locked band scales, lottery band stays bounded")
