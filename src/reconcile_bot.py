"""Reconcile the live bot's pricing against the offline backtest.

The backtest and the bot must compute the SAME z and the SAME fair value
from the same inputs, or paper results mean nothing. This drives a real
BotState with synthetic-but-realistic feeds and compares its output to the
closed-form the backtest uses, then checks the paper broker settles on the
verified contract rather than the old one.

Run: python3 src/reconcile_bot.py
"""
import os
import sys
import time

import numpy as np
from scipy.stats import norm

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.state import BotState, MarketState  # noqa: E402
from bot.strategy import RollAvgEdge  # noqa: E402
from bot.paper import PaperBroker  # noqa: E402

W = 30.0
FEE = 0.07


def offline_z(path, t0, t1, tau, K, sigma):
    """Exactly the backtest's formula (src/endgame_rollavg.py)."""
    rem = t1 - tau
    spot = path[tau]
    if tau <= t1 - W:
        s = max((t1 - W) - tau, 0.0)
        return (spot - K) / (sigma * np.sqrt(s + W / 3.0))
    S = sum(path[u] for u in range(int(t1 - W), int(tau)))
    return (S + rem * spot - W * K) / (sigma * np.sqrt(rem ** 3 / 3.0))


def main():
    rng = np.random.default_rng(7)
    t0 = (int(time.time()) // 300) * 300          # a real 5m boundary
    t1 = t0 + 300
    base = 65000.0
    # a driftless 1s path covering [t0-60, t1]
    secs = list(range(t0 - 60, t1 + 1))
    steps = rng.normal(0, 3.0, len(secs)).cumsum()
    path = {s: base + steps[i] for i, s in enumerate(secs)}

    st = BotState()
    # BotState warm-starts from the recorder's real oracle log and from live
    # klines. Both must be cleared here or real ticks land inside the
    # synthetic market's strike window and the comparison is meaningless.
    st.oracle_hist.clear()
    st.vol.var = (3.0 / base) ** 2               # 3 dollars per sqrt(sec)
    st.vol.n = 10_000
    st.binance_px = None
    m = MarketState("recon-5m", "tok_up", t0 * 1_000_000, t1 * 1_000_000,
                    asset_id_dn="tok_dn")
    st.markets[m.slug] = m

    # feed oracle + binance exactly as the live feeds would, 1/s
    for s in secs:
        st.on_oracle(path[s], s * 1000)
        st.binance_px = path[s]
        st.binance_us = s * 1_000_000
        st.basis.append(1.0)                      # oracle == binance here

    K_off = float(np.mean([path[u] for u in range(t0 - int(W), t0)]))
    K_bot = st.strike_avg(m)
    print(f"strike  offline {K_off:.4f}   bot {K_bot:.4f}   "
          f"diff {abs(K_off - K_bot):.2e}")
    assert abs(K_off - K_bot) < 1e-6, "strike mismatch"

    sigma = (st.vol.var ** 0.5) * base
    print(f"\n{'tau(rem)':>10} {'z_offline':>11} {'z_bot':>11} {'dz':>10} "
          f"{'fair_off':>10} {'fair_bot':>10} {'dfair':>10}")
    worst_z = worst_f = 0.0
    for rem in (120, 60, 30, 20, 10, 5, 2):
        tau = t1 - rem
        st.binance_px = path[tau]
        sig_t = (st.vol.var ** 0.5) * path[tau]
        zo = offline_z(path, t0, t1, tau, K_off, sig_t)
        zb = st.zscore(m, tau * 1_000_000)
        fo = float(norm.cdf(zo))
        fb = st.fair(m, tau * 1_000_000)
        worst_z = max(worst_z, abs(zo - zb))
        worst_f = max(worst_f, abs(fo - fb))
        print(f"{tau - t0:>7}({rem:>3}) {zo:>11.6f} {zb:>11.6f} "
              f"{abs(zo-zb):>10.2e} {fo:>10.6f} {fb:>10.6f} "
              f"{abs(fo-fb):>10.2e}")
    print(f"\nworst |dz| = {worst_z:.3e}   worst |dfair| = {worst_f:.3e}")
    assert worst_z < 1e-9 and worst_f < 1e-9, "bot/backtest pricing diverges"
    print("PASS: bot z and fair are bit-identical to the backtest formula")

    # settlement must use the verified contract, not the old rule
    settle = float(np.mean([path[u] for u in range(int(t1 - W), int(t1))]))
    want_up = settle >= K_off
    old_rule_up = path[t1] >= path[t0]
    r_sum, r_n = st.settle_sum_so_far(m, t1 * 1_000_000)
    got_up = (r_sum / r_n) >= K_bot
    print(f"\nsettlement: trailing-TWAP says Up={want_up}  bot says Up={got_up}"
          f"   (old rule would say Up={old_rule_up})")
    assert want_up == got_up, "settlement rule mismatch"
    print("PASS: paper settlement uses the verified trailing-TWAP contract")

    # strategy gate + broker round trip
    strat = RollAvgEdge({"zmin": 2.0, "edge_min": 0.02, "size": 100,
                         "window_s": 60, "min_rem_s": 2.0})
    tau = t1 - 15
    st.binance_px = path[tau]
    z = st.zscore(m, tau * 1_000_000)
    fv = st.fair(m, tau * 1_000_000)
    # place a book that is stale by 6 cents on the favoured side
    if z > 0:
        m.bids = [(round(fv - 0.10, 2), 300.0)]
        m.asks = [(round(min(fv - 0.06, 0.96), 2), 250.0)]
    else:
        m.bids = [(round(max(1 - (1 - fv) + 0.06, 0.04), 2), 250.0)]
        m.asks = [(round(1 - (1 - fv) + 0.10, 2), 300.0)]
    sig = strat.evaluate(st, m, tau * 1_000_000)
    print(f"\nz={z:+.2f} fair={fv:.4f} book bid={m.bids[0]} ask={m.asks[0]}")
    print(f"signal: {sig}")
    assert sig is not None and sig["action"] == "taker_buy", "no signal fired"
    br = PaperBroker(log_path="logs/reconcile_fills.jsonl")
    br.taker_buy(m.slug, sig["side"], sig["px"], sig["avail"], sig["size"],
                 meta={"reason": "reconcile"})
    pos = br.positions.get((m.slug, sig["side"]))
    print(f"paper fill: {pos}")
    assert pos and pos["shares"] > 0, "paper broker did not fill"
    eff = pos["cost"] / pos["shares"]
    fee_c = FEE * sig["px"] * (1 - sig["px"])
    print(f"effective cost/share {eff:.5f} vs px {sig['px']:.4f} + fee "
          f"{fee_c:.5f} = {sig['px'] + fee_c:.5f}")
    assert abs(eff - (sig["px"] + fee_c)) < 1e-6, \
        "paper broker fee does not match the verified schedule"
    print("PASS: paper broker charges the verified taker fee")

    # ---- feed-rate invariance -------------------------------------------
    # The backtest uses Binance 1s klines, where summing samples IS the
    # integral. The live oracle does not tick at 1 Hz, so a tick-sum scales
    # with feed rate and destroys z (observed live: z=-5322 at rem=23s).
    # Re-run the same market at 3 Hz and 1/3 Hz: z must be ~unchanged.
    base_z = None
    for rate, label in ((1.0, "1 Hz"), (3.0, "3 Hz"), (1 / 3.0, "0.33 Hz")):
        st2 = BotState()
        st2.oracle_hist.clear()
        st2.vol.var = (3.0 / base) ** 2
        st2.vol.n = 10_000
        st2.basis.clear()
        m2 = MarketState("recon-rate", "tok_up", t0 * 1_000_000,
                         t1 * 1_000_000, asset_id_dn="tok_dn")
        st2.markets[m2.slug] = m2
        step_us = int(1_000_000 / rate)
        ts = (t0 - 60) * 1_000_000
        while ts <= t1 * 1_000_000:
            sec = ts // 1_000_000
            st2.on_oracle(path[min(max(sec, t0 - 60), t1)], ts // 1000)
            ts += step_us
        st2.binance_px = path[t1 - 15]
        st2.basis.extend([1.0] * 600)
        z2 = st2.zscore(m2, (t1 - 15) * 1_000_000)
        if base_z is None:
            base_z = z2
        drift = abs(z2 - base_z) / max(abs(base_z), 1e-9)
        print(f"  oracle at {label:>8}: z = {z2:+.4f}   drift vs 1 Hz "
              f"{drift:.2%}")
        assert drift < 0.05, f"z depends on oracle tick rate ({label})"
    print("PASS: z is invariant to oracle feed rate (time-weighted integral)")

    print("\nALL RECONCILIATION CHECKS PASSED")


if __name__ == "__main__":
    main()
