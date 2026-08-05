"""Paper reproduction (H1) + estimator race (H15/H16) on the decision grid.

Train: markets with t0 < 2026-06-15. Test: the rest (reported only when
asked with 'test'). All fair values use only causal inputs.

Usage: python3 src/analysis_paper.py 15m [test]
"""
import sys

import numpy as np
import pandas as pd
from scipy.stats import norm, t as student_t

SPLIT_US = int(pd.Timestamp("2026-06-15").value // 1000)
FEE = 0.07

EST = ["rv5m", "rv15m", "rv1h", "rv4h", "rv24h", "bpv15m",
       "ewma1m", "ewma5m", "ewma30m"]


def load(horizon):
    g = pd.read_parquet(f"data/grid_{horizon}.parquet")
    g = g[g.spot_cl.notna() & g.strike.notna() & (g.rem_s > 0)].copy()
    g["y"] = (g.result == 0).astype(float)
    g["mid"] = (g.ask_tape + g.bid_tape) / 2
    g["spread"] = g.ask_tape - g.bid_tape
    g["fresh"] = (g.trade_age_s < 10) & g.spread.between(0, 0.03)
    g["train"] = g.t0_us < SPLIT_US
    return g


def calibrate(g):
    """Per-estimator variance scale on train grid points."""
    scales = {}
    tr = g[g.train]
    y = np.log(tr.settle_px / tr.spot_cl) ** 2
    for e in EST:
        x = tr[e] * tr.rem_s
        ok = (~(np.isnan(y) | np.isnan(x))) & (x > 0)
        yy, xx = y[ok], x[ok]
        q = np.quantile(yy, 0.995)
        m = yy < q
        scales[e] = float(np.sum(yy[m]) / np.sum(xx[m]))
    return scales


def fv_gauss(g, e, scale, season=False):
    v = g[e].values * scale * g.rem_s.values
    if season:
        v = v * g.season_factor.values
    v = np.maximum(v, 1e-14)
    sd = np.sqrt(v)
    return norm.cdf((np.log(g.spot_cl.values / g.strike.values) - 0.5 * v) / sd)


def fv_t(g, e, scale, nu=4.0):
    v = np.maximum(g[e].values * scale * g.rem_s.values, 1e-14)
    sd = np.sqrt(v * (nu - 2) / nu)
    z = (np.log(g.strike.values / g.spot_cl.values) + 0.5 * v) / sd
    return 1.0 - student_t.cdf(z, df=nu)


def brier(p, y):
    ok = ~np.isnan(p)
    return float(np.mean((p[ok] - y[ok]) ** 2)), int(ok.sum())


def race(g, scales, label):
    f = g[g.fresh & g.mid.notna()]
    print(f"\n=== ESTIMATOR RACE {label}: {len(f)} pts, "
          f"{f.slug.nunique()} mkts ===")
    bm, n = brier(f.mid.values, f.y.values)
    print(f"market mid Brier: {bm:.5f} (n={n})")
    rows = []
    for e in EST:
        p = fv_gauss(f, e, scales[e])
        b, _ = brier(p, f.y.values)
        pt = fv_t(f, e, scales[e])
        bt, _ = brier(pt, f.y.values)
        ps = fv_gauss(f, e, scales[e], season=True)
        bs, _ = brier(ps, f.y.values)
        rows.append((e, scales[e], b, bt, bs))
    res = pd.DataFrame(rows, columns=["est", "scale", "brier_gauss",
                                      "brier_t4", "brier_seasonal"])
    res["vs_market"] = res.brier_gauss - bm
    print(res.round(5).to_string(index=False))
    # by phase for the best estimator
    best = res.sort_values("brier_gauss").iloc[0].est
    f2 = f.copy()
    f2["phase"] = pd.cut(f2.tau_s / (f2.tau_s + f2.rem_s),
                         [0, 0.33, 0.66, 0.9, 1.0],
                         labels=["early", "mid", "late", "final"])
    p = fv_gauss(f2, best, scales[best])
    f2["model_p"] = p
    for ph, d in f2.groupby("phase", observed=True):
        b1, _ = brier(d.model_p.values, d.y.values)
        b2, _ = brier(d.mid.values, d.y.values)
        print(f"phase {ph}: model {b1:.5f} vs market {b2:.5f} "
              f"({'model' if b1 < b2 else 'market'} wins, n={len(d)})")
    return best


def paper_rule(g, scales, est, label, buffer=0.02):
    """H1: trade when |fv - mid| >= buffer, taker at tape touch, hold to
    settlement, one trade per market (first signal)."""
    f = g[g.fresh & g.mid.notna()].copy()
    f["fv"] = fv_gauss(f, est, scales[est])
    f["div"] = f.fv - f.mid
    sig = f[np.abs(f.div) >= buffer].sort_values("t_us").groupby("slug").first()
    if len(sig) == 0:
        print(f"{label}: no signals")
        return
    buy_up = sig.div > 0
    entry = np.where(buy_up, sig.ask_tape, 1 - sig.bid_tape)
    win = np.where(buy_up, sig.y, 1 - sig.y)
    fee = FEE * entry * (1 - entry)
    pnl = win - entry - fee
    ret = pnl / entry
    # nulls on the same markets
    fav_up = sig.mid >= 0.5
    fav_entry = np.where(fav_up, sig.ask_tape, 1 - sig.bid_tape)
    fav_win = np.where(fav_up, sig.y, 1 - sig.y)
    fav_pnl = fav_win - fav_entry - FEE * fav_entry * (1 - fav_entry)
    rng = np.random.default_rng(11)
    rand_up = rng.random(len(sig)) < 0.5
    rand_entry = np.where(rand_up, sig.ask_tape, 1 - sig.bid_tape)
    rand_win = np.where(rand_up, sig.y, 1 - sig.y)
    rand_pnl = rand_win - rand_entry - FEE * rand_entry * (1 - rand_entry)
    B = 2000
    boots = [np.mean(rng.choice(pnl, len(pnl))) for _ in range(B)]
    lo, hi = np.quantile(boots, [0.025, 0.975])
    print(f"\n=== PAPER RULE {label}: {len(sig)} trades ===")
    print(f"mean entry {np.mean(entry):.3f}, win rate {np.mean(win):.4f}, "
          f"breakeven {np.mean(entry + fee):.4f}")
    print(f"net PnL/share {np.mean(pnl)*100:.2f}c  CI [{lo*100:.2f}, "
          f"{hi*100:.2f}]c ; return on stake {np.mean(ret)*100:.2f}%")
    print(f"favorite null: {np.mean(fav_pnl)*100:.2f}c/share; "
          f"random null: {np.mean(rand_pnl)*100:.2f}c/share")
    print(f"profit concentration: top10 = "
          f"{np.sort(pnl)[-10:].sum() / max(np.sum(pnl), 1e-9):.2f} of total")


if __name__ == "__main__":
    horizon = sys.argv[1]
    use_test = "test" in sys.argv[2:]
    g = load(horizon)
    scales = calibrate(g)
    tr = g[g.train]
    best = race(tr, scales, f"{horizon} TRAIN")
    paper_rule(tr, scales, "ewma5m", f"{horizon} TRAIN ewma5m")
    paper_rule(tr, scales, best, f"{horizon} TRAIN {best}")
    if use_test:
        te = g[~g.train]
        race(te, scales, f"{horizon} TEST")
        paper_rule(te, scales, best, f"{horizon} TEST {best}")
