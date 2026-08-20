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

from bot.state import BotState, MarketState, OnlineVol  # noqa: E402
from bot.strategy import RollAvgEdge  # noqa: E402
from bot.paper import PaperBroker  # noqa: E402
from bot.calib import p_up  # noqa: E402

W = 30.0
FEE = 0.07


def offline_z(path, t0, t1, tau, K, sigma):
    """The backtest's formula (src/endgame_rollavg.py) with one deliberate
    live refinement: the trailing elapsed second, for which no oracle
    round has been DELIVERED yet at the decision instant (rounds arrive
    1.6-2.8s after their stamps), is priced at current spot rather than
    at the last held round -- the same clock the future leg uses."""
    rem = t1 - tau
    spot = path[tau]
    if tau <= t1 - W:
        s = max((t1 - W) - tau, 0.0)
        return (spot - K) / (sigma * np.sqrt(s + W / 3.0))
    S = sum(path[u] for u in range(int(t1 - W), int(tau) - 1)) + spot
    return (S + rem * spot - W * K) / (sigma * np.sqrt(rem ** 3 / 3.0))


def main():
    rng = np.random.default_rng(7)
    # Construct BotState FIRST: its warm-start pages the public kline API
    # and can take tens of seconds. Stamping the clock before that lets the
    # synthetic oracle ticks age past the 20s frozen-oracle guard while the
    # constructor is still running -- a flaky failure in the harness.
    st = BotState()
    st.oracle_hist.clear()
    # Anchor the synthetic window so its LAST oracle tick lands at now.
    t1 = int(time.time())
    t0 = t1 - 300
    base = 65000.0
    secs = list(range(t0 - 60, t1 + 1))
    steps = rng.normal(0, 3.0, len(secs)).cumsum()
    path = {s_: base + steps[i] for i, s_ in enumerate(secs)}
    st.vol.force_var((3.0 / base) ** 2)          # 3 dollars per sqrt(sec)
    st.oracle_sigma_rel = lambda *a, **k: None   # force Binance fallback
    # The fallback now refuses to run without a trusted oracle reference
    # (cold-start guard), so the harness must supply one, exactly as a
    # warmed-up live process would have.
    st._last_good_sigma = 3.0 / base
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
        # fair is the EMPIRICAL calibration of z, not Phi(z): Phi is
        # measurably overconfident here (z>3 settles Up 91.6%, not 99.87%).
        fo = float(p_up(zo))
        fb = st.fair(m, tau * 1_000_000)
        worst_z = max(worst_z, abs(zo - zb))
        worst_f = max(worst_f, abs(fo - fb))
        print(f"{tau - t0:>7}({rem:>3}) {zo:>11.6f} {zb:>11.6f} "
              f"{abs(zo-zb):>10.2e} {fo:>10.6f} {fb:>10.6f} "
              f"{abs(fo-fb):>10.2e}")
    print(f"\nworst |dz| = {worst_z:.3e}   worst |dfair| = {worst_f:.3e}")
    assert worst_z < 1e-9 and worst_f < 1e-9, "bot/backtest pricing diverges"
    print("PASS: bot z is bit-identical to the backtest formula, and fair "
          "is its empirical calibration")

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
        st2.vol.force_var((3.0 / base) ** 2)
        st2.oracle_sigma_rel = lambda *a, **k: None
        st2._last_good_sigma = 3.0 / base
        st2.basis.clear()
        m2 = MarketState("recon-rate", "tok_up", t0 * 1_000_000,
                         t1 * 1_000_000, asset_id_dn="tok_dn")
        st2.markets[m2.slug] = m2
        step_us = int(1_000_000 / rate)
        ts = (t0 - 60) * 1_000_000
        # feed up to NOW, not to t1: at 0.33 Hz the last tick can land 3s
        # short of t1, and the seconds this test itself takes then push the
        # newest round past the 20s frozen-oracle guard.
        end_us = max(t1, int(time.time())) * 1_000_000
        while ts <= end_us:
            sec = ts // 1_000_000
            st2.on_oracle(path[min(max(sec, t0 - 60), t1)], ts // 1000)
            ts += step_us
        st2.binance_px = path[t1 - 15]
        st2.basis.extend([1.0] * 600)
        z2 = st2.zscore(m2, (t1 - 15) * 1_000_000)
        assert z2 is not None, f"{label}: zscore returned None"
        if base_z is None:
            base_z = z2
        drift = abs(z2 - base_z) / max(abs(base_z), 1e-9)
        print(f"  oracle at {label:>8}: z = {z2:+.4f}   drift vs 1 Hz "
              f"{drift:.2%}")
        assert drift < 0.05, f"z depends on oracle tick rate ({label})"
    print("PASS: z is invariant to oracle feed rate (time-weighted integral)")

    # ---- the bot's OWN sigma must equal the backtest's estimator ---------
    # Injecting the same sigma into both sides (as every check above does)
    # cannot catch an estimator mismatch. The backtest uses
    #   log(px).diff().rolling(3600, min_periods=600).std()
    # so feed a known path through the live updater and compare.
    import pandas as pd
    rng2 = np.random.default_rng(11)
    n = 5000
    px_path = 65000 * np.exp(rng2.normal(0, 1.3e-5, n).cumsum())
    v = OnlineVol()
    t_epoch = 1_700_000_000
    for i, p_ in enumerate(px_path):
        v.update(t_epoch + i, float(p_))
    offline = pd.Series(px_path).apply(np.log).diff().rolling(
        3600, min_periods=600).std().iloc[-1]
    live = np.sqrt(v.var)
    print(f"\n  sigma  offline rolling(3600).std() {offline:.6e}")
    print(f"  sigma  bot OnlineVol             {live:.6e}   "
          f"rel diff {abs(live-offline)/offline:.2%}")
    assert abs(live - offline) / offline < 0.01, \
        "live vol estimator does not match the backtest's rolling std"
    print("PASS: bot sigma IS the backtest's 1h trailing std of 1s returns")

    # ---- book gate: one-sided books must not veto the tradeable side ----
    # Live, a two-sided-Up-book requirement rejected 48 of 48 priced
    # evaluations: in the settle window a near-decided market loses the bid
    # on the losing token and quotes 0.001, which is exactly when the OTHER
    # token is worth buying.
    # Construct FIRST: the kline warm-start takes seconds, and stamping
    # the window before it would age the synthetic ticks past the 3.5s
    # stale-spot guard (same flake the main section documents).
    sb = BotState()
    t1b = int(time.time())
    t0b = t1b - 300
    sb.oracle_hist.clear()
    sb.vol.force_var((3.0 / base) ** 2)
    sb.oracle_sigma_rel = lambda *a, **k: None
    sb._last_good_sigma = 3.0 / base
    sb.basis.extend([1.0] * 600)
    for u in range(t0b - 40, t1b):
        sb.on_oracle(65000.0 if u < t1b - 30 else 64900.0, u * 1000)
    sb.binance_px = 64900.0
    mb = MarketState("gate-5m", "up", t0b * 1_000_000, t1b * 1_000_000,
                     asset_id_dn="dn")
    sb.markets[mb.slug] = mb
    taub = (t1b - 10) * 1_000_000
    cfg = {"zmin": 2.0, "size": 100, "max_price": 0.97, "min_rem_s": 2.0,
           "window_s": 60, "cooldown_s": 1.0}
    mb.bids, mb.asks = [], [(0.001, 500.0)]          # Up side dead
    mb.bids_dn, mb.asks_dn = [(0.90, 300.0)], [(0.93, 250.0)]
    g = RollAvgEdge(cfg).evaluate(sb, mb, taub)
    assert g and g["side"] == "Down" and abs(g["px"] - 0.93) < 1e-9, \
        "one-sided Up book vetoed a tradeable Down side"
    mb.asks, mb.asks_dn, mb.bids, mb.bids_dn = [], [], [], []
    assert RollAvgEdge(cfg).evaluate(sb, mb, taub) is None, \
        "fired with no ask anywhere"
    mb.asks_dn = [(0.99, 400.0)]
    assert RollAvgEdge(cfg).evaluate(sb, mb, taub) is None, \
        "fired on an ask richer than max_price"
    print("PASS: book gate trades the live side, refuses empty/rich books")

    # ---- book must track price_change DELTAS, not just snapshots --------
    # 97.2% of CLOB messages are price_change and 1.9% are `book`. A bot
    # that reads snapshots only is blind to nearly every book change, and
    # this strategy trades transient dips that arrive as deltas.
    sd = BotState()
    sd.oracle_hist.clear()
    t0d = int(time.time()) - 300
    md = MarketState("delta-5m", "up", t0d * 1_000_000,
                     (t0d + 300) * 1_000_000, asset_id_dn="dn")
    sd.markets[md.slug] = md
    sd.on_book("up", [(0.90, 100.0)], [(0.93, 200.0)], None)
    assert md.best_ask()[0] == 0.93
    sd.on_price_change("up", 0.92, 150.0, "SELL")
    assert md.best_ask() == (0.92, 150.0), "delta did not improve the ask"
    sd.on_price_change("up", 0.92, 0.0, "SELL")
    assert md.best_ask()[0] == 0.93, "pulled level not removed"
    sd.on_price_change("up", 0.91, 75.0, "BUY")
    assert md.best_bid() == (0.91, 75.0), "delta did not improve the bid"
    sd.on_price_change("dn", 0.05, 300.0, "SELL")
    assert md.best_ask_dn()[:2] == (0.05, 300.0), "down book not tracked"
    print("PASS: order book tracks price_change deltas, both tokens")

    # ---- latency-delayed fills ------------------------------------------
    # Zero-latency fills were the single biggest way paper flattered
    # reality: 63% of an overnight +$1,331 came from sub-0.85 dips, which
    # are exactly the prices that vanish fastest. Orders must wait, then
    # execute against the book AS IT IS THEN, capped at the signal price.
    from bot.run import Bot                                   # noqa: E402
    from bot.risk import Risk                                 # noqa: E402
    bt = Bot.__new__(Bot)
    bt.broker = PaperBroker(log_path="logs/reconcile_fills.jsonl")
    bt.risk = Risk({})
    bt.decisions = open("logs/reconcile_dec.jsonl", "a")
    bt.pending, bt.pending_settle, bt.n_miss = [], {}, 0
    bt.inflight, bt.mode = [], "paper"   # live-mode plumbing, off here
    bt.n_reject = bt.n_partial = bt.n_sent = 0
    bt.n_miss_why = {"ask_gone": 0, "too_small": 0, "no_market": 0}
    bt.latency_us = 150_000
    # participation 1.0 / no tape cap / no rejects isolates the LATENCY
    # behaviour; the caps get their own checks below.
    bt.fill_cfg = {"participation": 1.0, "vol_participation": 0.25,
                   "vol_window_s": 5.0, "reject_rate": 0.0,
                   "min_order_size": 5.0, "use_tape_cap": False}
    bt.state = BotState()
    bt.state.oracle_hist.clear()
    t1l = int(time.time()) + 300
    ml = MarketState("lat-5m", "up", (t1l - 300) * 1_000_000,
                     t1l * 1_000_000, asset_id_dn="dn")
    bt.state.markets[ml.slug] = ml
    ml.set_book(True, [(0.80, 100.0)], [(0.84, 200.0)])
    from bot.state import now_us as _n
    ml.book_us = _n()      # the broker refuses fills on a stale book view

    def _q(limit, size, due_us):
        bt.pending.append({"slug": ml.slug, "side": "Up", "limit": limit,
                           "size": size, "fire_us": due_us,
                           "meta": {"reason": "rollavg recon"}})
    _q(0.84, 100, _n() - 1); bt._process_pending()
    assert bt.broker.positions[(ml.slug, "Up")]["shares"] == 100, \
        "did not fill when the ask was still there"
    ml.apply_delta(True, 0.84, 0.0, "SELL")
    ml.apply_delta(True, 0.95, 300.0, "SELL")
    _q(0.84, 100, _n() - 1); bt._process_pending()
    assert bt.n_miss == 1, "pulled ask must count as a miss"
    assert bt.broker.positions[(ml.slug, "Up")]["shares"] == 100, \
        "filled above the limit price"
    _q(0.99, 10, _n() + 5_000_000); bt._process_pending()
    assert len(bt.pending) == 1, "filled before the latency elapsed"
    bt.pending.clear()
    print("PASS: taker fills wait for latency, miss when the ask is pulled, "
          "and never exceed the limit")

    # ---- execution realism ----------------------------------------------
    # Recorded books say that 400ms after we look, the price we aimed at is
    # reachable ~65% of the time in the 0.92-0.99 band and about half the
    # size survives. Taking 100% of displayed depth, every time, was the
    # broker's largest remaining fiction.
    def fresh(book, tape=(), **cfg):
        bt.broker.positions.clear()
        bt.pending.clear()
        bt.n_miss = bt.n_reject = bt.n_partial = 0
        for k in bt.n_miss_why:
            bt.n_miss_why[k] = 0
        # The market must be INSIDE its settle window: the claim ledger
        # counts prints from [t1-w, now) only, which is the only region
        # the strategy can queue orders from anyway.
        t1e = int(time.time()) + 20
        m2 = MarketState("exec-5m", "up", (t1e - 300) * 1_000_000,
                         t1e * 1_000_000, asset_id_dn="dn")
        bt.state.markets["exec-5m"] = m2
        m2.set_book(True, [(0.80, 100.0)], book)
        m2.book_us = _n()
        for px, sz in tape:
            m2.tape.append((_n(), px, sz))
        bt.fill_cfg = {"participation": 1.0, "vol_participation": 0.25,
                       "vol_window_s": 5.0, "reject_rate": 0.0,
                       "min_order_size": 5.0, "use_tape_cap": False, **cfg}
        return m2

    def take(m2, limit, size):
        bt.pending.append({"slug": m2.slug, "side": "Up", "limit": limit,
                           "size": size, "fire_us": _n() - 1,
                           "meta": {"reason": "rollavg recon"}})
        bt._process_pending()
        return bt.broker.positions.get((m2.slug, "Up"), {"shares": 0.0,
                                                         "cost": 0.0})

    m2 = fresh([(0.90, 100.0)], participation=0.5)
    assert take(m2, 0.90, 100)["shares"] == 50.0, \
        "participation cap not applied to displayed size"
    assert bt.n_partial == 1, "short fill not counted as partial"
    print("PASS: we win only `participation` of the displayed size")

    # walking the book: 40 at 0.90 then the rest at 0.93, not all at 0.90
    m2 = fresh([(0.90, 40.0), (0.93, 500.0)])
    pos = take(m2, 0.93, 100)
    assert pos["shares"] == 100.0, f"expected 100 shares, got {pos}"
    vw = (40 * 0.90 + 60 * 0.93) / 100
    got_vw = sum(s * p for p, s in [(0.90, 40), (0.93, 60)]) / 100
    assert abs(vw - got_vw) < 1e-12
    # cost carries the fee, so compare the share-weighted price implied
    fee = 0.07 * 0.90 * 0.10 * 40 + 0.07 * 0.93 * 0.07 * 60
    assert abs(pos["cost"] - (vw * 100 + fee)) < 1e-6, \
        f"order did not walk the book: cost {pos['cost']}"
    print("PASS: size beyond the touch walks up the book and pays for it")

    # our own take removes the liquidity we just consumed
    assert m2.lv_up["ask"].get(0.90, 0.0) == 0.0, "level not consumed"
    assert abs(m2.lv_up["ask"][0.93] - 440.0) < 1e-9, "wrong amount consumed"
    print("PASS: our fill removes the liquidity it took")

    # tape cap: 200 displayed but only 40 printed -> 0.25*40 = 10 shares
    m2 = fresh([(0.90, 200.0)], tape=[(0.90, 40.0)], use_tape_cap=True)
    assert take(m2, 0.90, 100)["shares"] == 10.0, \
        "fill exceeded a quarter of what actually printed"
    # nothing printed at all -> nothing to take
    m2 = fresh([(0.90, 200.0)], use_tape_cap=True)
    assert take(m2, 0.90, 100)["shares"] == 0.0 and bt.n_miss == 1, \
        "filled against depth that never traded"
    print("PASS: fills are capped by realised prints, not displayed offers")

    # LIFETIME cap: the venue restores the book (our paper fill never
    # happened out there), but the same 40 printed shares must not justify
    # a second 10-share claim -- one print, one 25% share, ever.
    m2 = fresh([(0.90, 200.0)], tape=[(0.90, 40.0)], use_tape_cap=True)
    assert take(m2, 0.90, 100)["shares"] == 10.0
    m2.set_book(True, [(0.80, 100.0)], [(0.90, 200.0)])   # venue refresh
    m2.book_us = _n()
    pos = take(m2, 0.90, 100)
    assert pos["shares"] == 10.0 and bt.n_miss == 1, \
        f"same prints claimed twice across a book refresh: {pos}"
    print("PASS: a book refresh cannot resurrect liquidity we already took")

    # killed bot must also drop orders already inside the latency window
    m2 = fresh([(0.90, 200.0)], tape=[(0.90, 400.0)], use_tape_cap=True)
    bt.risk.killed = True
    pos = take(m2, 0.90, 100)
    assert pos["shares"] == 0.0 and not bt.pending, \
        "kill switch let a queued order execute"
    bt.risk.killed = False
    assert take(m2, 0.90, 100)["shares"] > 0, "un-kill did not restore"
    print("PASS: the kill switch flushes the pending queue too")

    # opposite-side guard must see PENDING orders, not just filled ones:
    # z can flip sign inside the latency window and buying both sides pays
    # ~1.9 for a 1.0 payoff.
    class _OppStub:
        def evaluate(self, state, m, t_us):
            return {"action": "taker_buy", "side": "Down", "px": 0.90,
                    "avail": 100.0, "size": 100, "reason": "rollavg stub"}
    mo = MarketState("opp-5m", "up", (t1l - 300) * 1_000_000,
                     t1l * 1_000_000, asset_id_dn="dn")
    bt.state.markets[mo.slug] = mo
    mo.set_book(True, [(0.85, 100.0)], [(0.90, 200.0)])
    mo.book_us = _n()
    bt.state.oracle_us = _n()
    bt.state.binance_us = _n()
    bt.strategies = [_OppStub()]
    bt.n_eval = bt.n_signal = bt.n_eval_err = 0
    bt.n_rej = {"binance": 0, "oracle": 0, "book": 0, "killed": 0,
                "size": 0}
    bt.latency_us = 5_000_000
    bt.pending = [{"slug": mo.slug, "side": "Up", "limit": 0.90,
                   "size": 50, "fire_us": _n() + 4_000_000, "meta": {}}]
    bt._try_market_inner(mo)
    assert all(o["side"] == "Up" for o in bt.pending), \
        "queued Down while an Up order was still pending"
    bt.pending = []
    bt._try_market_inner(mo)
    assert any(o["side"] == "Down" for o in bt.pending), \
        "guard now blocks even with nothing pending"
    bt.pending = []
    print("PASS: opposite-side guard covers orders still in flight")

    # one_shot semantics (EarlyBird): ONE entry per market. A partial
    # fill must not top up at a worse price (that execution profile was
    # never measured), but a clean kill (zero shares, nothing queued)
    # may retry inside the window -- the measured "first print after
    # open" semantics.
    class _OneShotStub:
        def evaluate(self, state, m, t_us):
            return {"action": "taker_buy", "side": "Up", "px": 0.90,
                    "avail": 100.0, "size": 15, "one_shot": True,
                    "reason": "earlybird stub"}
    bt.strategies = [_OneShotStub()]
    bt.broker.positions.pop((mo.slug, "Up"), None)
    bt.broker.positions.pop((mo.slug, "Down"), None)
    bt._try_market_inner(mo)                      # clean: fires
    assert sum(1 for o in bt.pending if o["slug"] == mo.slug) == 1, \
        "one_shot blocked a clean first entry"
    bt._try_market_inner(mo)                      # in flight: blocked
    assert sum(1 for o in bt.pending if o["slug"] == mo.slug) == 1, \
        "one_shot re-fired over an order still in the queue"
    bt.pending = []
    bt.broker.positions[(mo.slug, "Up")] = {"shares": 9.0, "cost": 4.1}
    bt._try_market_inner(mo)                      # partial fill: blocked
    assert not bt.pending, \
        "one_shot topped up a partially filled market"
    bt.broker.positions.pop((mo.slug, "Up"), None)
    bt._try_market_inner(mo)                      # killed order: retries
    assert sum(1 for o in bt.pending if o["slug"] == mo.slug) == 1, \
        "one_shot refused a retry after a no-fill kill"
    bt.pending = []
    print("PASS: one_shot -- no top-ups after fills, retries after kills")

    # the event-driven eval path must cover the OPEN window when
    # EarlyBird is registered (its edge lives in the first seconds; the
    # 1 Hz safety net alone gives away the opening book race)
    t_now = _n()
    m_open = MarketState("open-5m", "up", t_now - 10_000_000,
                         t_now + 290_000_000, asset_id_dn="dn2")
    bt.state.markets[m_open.slug] = m_open
    bt._open_eval_s = 0.0
    n0 = bt.n_eval
    bt._maybe_eval(m_open)                        # rem 290s, no open eval
    assert bt.n_eval == n0, "evaluated outside settle window w/o opt-in"
    bt._open_eval_s = 47.0
    bt._maybe_eval(m_open)                        # elapsed 10s <= 47
    assert bt.n_eval == n0 + 1, "open-window book event not evaluated"
    m_open.t0_us = t_now - 60_000_000             # elapsed 60s > 47
    bt._maybe_eval(m_open)
    assert bt.n_eval == n0 + 1, "evaluated past the open window"
    bt._open_eval_s = 0.0
    bt.state.markets.pop(m_open.slug, None)
    print("PASS: book events evaluate the open window only when opted in")

    # the cash-floor day anchor must survive a restart: reload the day's
    # FIRST anchor, not the post-drawdown balance a restart would see
    import tempfile
    bt.logdir = tempfile.mkdtemp(prefix="recon_anchor_")
    old_dec = bt.decisions
    bt.decisions = open(os.path.join(bt.logdir, "decisions.jsonl"), "a")
    bt._day_bal_anchor = None
    bt.log_decision({"kind": "day_anchor",
                     "day": time.strftime("%Y-%m-%d", time.gmtime()),
                     "bal": 93.5})
    bt.log_decision({"kind": "day_anchor",
                     "day": time.strftime("%Y-%m-%d", time.gmtime()),
                     "bal": 60.0})                # later, post-loss
    bt._seed_day_pnl()
    bt.decisions.close()
    bt.decisions = old_dec
    assert bt._day_bal_anchor is not None \
        and abs(bt._day_bal_anchor[1] - 93.5) < 1e-9, \
        f"anchor reload took the wrong line: {bt._day_bal_anchor}"
    print("PASS: cash-floor anchor survives restarts at the day's first "
          "balance")

    # below the venue's own orderMinSize=5 we cannot send an order at all
    m2 = fresh([(0.90, 6.0)], participation=0.5)
    assert take(m2, 0.90, 100)["shares"] == 0.0 and bt.n_miss == 1, \
        "sent an order below the venue minimum size"
    print("PASS: sub-minimum orders are misses, not fills")

    m2 = fresh([(0.90, 200.0)], reject_rate=1.0)
    assert take(m2, 0.90, 100)["shares"] == 0.0 and bt.n_reject == 1, \
        "venue rejection not modelled"
    print("PASS: venue re-validation rejections are counted, not filled")

    # ---- halt ladder ----------------------------------------------------
    # A halt exists to bound the loss when the EDGE breaks, not to punish
    # variance: streak trip -> cool-off -> half-size probe -> kill or
    # restore, and the whole state must survive a restart via seed().
    ok_st = {"oracle_s": 0.1}
    r1 = Risk({"daily_loss_limit": 1e9,
               "halt": {"streak_n": 5, "streak_losses": 3, "cooloff_s": 0.5,
                        "resume_frac": 0.5, "probe_markets": 2,
                        "probe_losses": 1}})
    for pnl in (5, -5, 5, -5, -5):          # 3 losses in the last 5
        r1.on_settle_pnl(pnl)
    assert not r1.inputs_ok(ok_st, 0.1), "streak breaker did not trip"
    time.sleep(0.6)
    assert r1.inputs_ok(ok_st, 0.1), "cool-off did not expire"
    assert r1.size_ok(0, 0, 100, 0.9, 0) == 50.0, "probe not at half size"
    r1.on_settle_pnl(-5)                    # probe loss -> breakage real
    assert r1.killed and r1.size_ok(0, 0, 100, 0.9, 0) == 0.0, \
        "failed probe did not kill"
    r2 = Risk({"daily_loss_limit": 1e9,
               "halt": {"streak_n": 5, "streak_losses": 3, "cooloff_s": 0.2,
                        "resume_frac": 0.5, "probe_markets": 2,
                        "probe_losses": 2}})
    for pnl in (-5, -5, 5, 5, -5):
        r2.on_settle_pnl(pnl)
    assert not r2.inputs_ok(ok_st, 0.1)
    time.sleep(0.3)
    assert r2.inputs_ok(ok_st, 0.1)
    r2.on_settle_pnl(5)
    r2.on_settle_pnl(5)
    assert r2.probe_left == 0 and r2.size_ok(0, 0, 100, 0.9, 0) == 100.0, \
        "clean probe did not restore full size"
    r3 = Risk({"halt": {"streak_n": 5, "streak_losses": 3,
                        "cooloff_s": 300}})
    r3.seed(-120.0, [True, False, False, True, False])
    assert not r3.inputs_ok(ok_st, 0.1), \
        "seeded streak did not re-arm the halt after a restart"
    print("PASS: halt ladder -- streak trip, cool-off, half-size probe, "
          "kill, restore, seed")

    print("\nALL RECONCILIATION CHECKS PASSED")


if __name__ == "__main__":
    main()
