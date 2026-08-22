"""A scalp signal must reach the exit loop as a tracked position.

The strategy emits `scalp` (exit target + deadline) and the fill handler
reads meta["scalp"] to register it. Both dispatch paths used to build
meta from a hand-written key list that did not name `scalp`, so it was
dropped in silence: _scalps stayed empty, the exit loop had nothing to
sell, and three live entries were carried to settlement and lost.

Nothing failed loudly -- the buy succeeded, the funnel showed `fired`,
and the only symptom was an exit that never happened. So this asserts
the whole chain: signal -> order meta -> registered position.

    python3 src/scalp_registration_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.run import sig_meta, _SIG_CONTROL      # noqa: E402
from bot.strategy import JumpScalp              # noqa: E402
from bot.state import MarketState, now_us       # noqa: E402
from collections import deque                   # noqa: E402


class StubState:
    def __init__(self, hist, spot, z):
        self.oracle_hist, self._spot, self._z = hist, spot, z
    def zscore(self, m, t_us=None): return self._z
    def spot_adj(self): return self._spot
    def oracle_age_s(self): return 1.0


def real_signal():
    now = now_us()
    hist = deque((now - k * 1_000_000, 100000.0 - k * 2.0)
                 for k in range(60, -1, -1))
    t0 = now - 1_000_000                       # opened 1s ago -> since=+1
    m = MarketState("btc-updown-5m-t", "up", t0, t0 + 300_000_000,
                    asset_id_dn="dn")
    m.set_book(True, [(0.49, 500)], [(0.51, 500)])
    m.set_book(False, [(0.49, 500)], [(0.51, 500)])
    js = JumpScalp({"coins": ["btc"], "zmin": 0.15, "size": 20,
                    "entry_lo": 0, "entry_hi": 3, "dir_mode": "both"})
    sig = js.evaluate(StubState(hist, hist[-1][1] + 2.0, 0.9), m, now)
    assert sig is not None, "strategy produced no signal"
    return sig


sig = real_signal()
assert "scalp" in sig, sig.keys()
print("signal carries scalp:", sig["scalp"])

for label, meta in (("live dispatch", sig_meta(sig, seen_px=sig["px"],
                                               pad_c=1.0)),
                    ("pending path", sig_meta(sig, seen_px=sig["px"]))):
    sc = (meta or {}).get("scalp")
    assert sc is not None, f"{label}: scalp DROPPED from meta -> the exit " \
                           f"loop never sees the position"
    assert "target" in sc and "deadline_us" in sc, sc
    print(f"  {label:<14} forwards scalp  target={sc['target']}")

# the fill handler's exact test
o = {"slug": "S", "side": sig["side"], "meta": sig_meta(sig, seen_px=0.51)}
assert (o.get("meta") or {}).get("scalp") is not None, \
    "fill handler would not register the position"
print("fill handler registers the position")

# control keys stay out; everything else is forwarded by default
m2 = sig_meta({"action": "taker_buy", "side": "Up", "size": 20, "px": 0.51,
               "reason": "r", "scalp": {"a": 1}, "brand_new_field": 7})
assert "action" not in m2 and "size" not in m2, m2
assert m2["brand_new_field"] == 7, "new strategy fields must survive"
print("control keys excluded, unknown fields forwarded:", sorted(m2))
print("\nPASS: a scalp signal reaches the exit loop as a tracked position")
