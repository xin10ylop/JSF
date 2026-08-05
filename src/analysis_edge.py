"""Venue-vs-model joint analysis on the decision grid.

1. Brier race: market mid vs G(z) vs Phi(z), by phase.
2. Divergence-trade EV tables (taker at touch, net fees), by |d| and phase.
3. (price, z) reconciliation: who is wrong where.

Usage: python3 src/analysis_edge.py 15m [test]
"""
import glob
import pickle
import sys

import numpy as np
import pandas as pd
from scipy.stats import norm

from fit_gz import predict_gz

SPLIT_US = int(pd.Timestamp("2026-06-15").value // 1000)
FEE = 0.07


def basis_ratio_series():
    cp = pd.read_parquet("data/telonex/chainlink_btcusd.parquet")
    cp_b = cp.sort_values("server_timestamp_us")
    cp_b["bsec"] = cp_b.server_timestamp_us // 1_000_000
    kp = cp_b.groupby("bsec").price_f.last()
    grid = np.arange(kp.index[0], kp.index[-1] + 1)
    kpf = kp.reindex(grid).ffill().values
    bn = pd.concat([pd.read_parquet(f, columns=["open_time", "close"])
                    for f in sorted(glob.glob("data/binance/parquet/*.parquet"))],
                   ignore_index=True)
    bn["sec"] = (bn.open_time // 1_000_000).astype("int64")
    bnp = bn.set_index("sec").close.reindex(grid).ffill().values
    ratio = pd.Series(kpf / bnp).rolling(600, min_periods=60).median().shift(1)
    return pd.Series(ratio.values, index=grid)


def load(horizon):
    g = pd.read_parquet(f"data/grid_{horizon}.parquet")
    g = g[g.spot_bn.notna() & g.strike.notna() & (g.rem_s > 0)].copy()
    g["y"] = (g.result == 0).astype(float)
    g["mid"] = (g.ask_tape + g.bid_tape) / 2
    g["spread"] = g.ask_tape - g.bid_tape
    g["fresh"] = (g.trade_age_s < 10) & g.spread.between(0, 0.03)
    g["train"] = g.t0_us < SPLIT_US
    ratio = basis_ratio_series()
    g["ratio"] = ratio.reindex(g.t_us // 1_000_000).values
    g["spot_adj"] = g.spot_bn * g.ratio
    K = 1.35
    g["z"] = (np.log(g.spot_adj / g.strike)
              / np.sqrt(K * g.ewma5m * g.rem_s))
    with open("data/gz_models.pkl", "rb") as f:
        gz = pickle.load(f)
    g["fv_g"] = predict_gz(gz, g.z.values, g.rem_s.values)
    g["fv_phi"] = norm.cdf(g.z.values)
    g["phase"] = pd.cut(g.tau_s / (g.tau_s + g.rem_s),
                        [0, 0.33, 0.66, 0.9, 1.0],
                        labels=["early", "mid", "late", "final"])
    return g


def brier_race(f, label):
    print(f"\n=== BRIER {label}: n={len(f)}, {f.slug.nunique()} mkts ===")
    for ph, d in f.groupby("phase", observed=True):
        rows = {}
        for name, col in [("market", "mid"), ("G(z)", "fv_g"),
                          ("Phi(z)", "fv_phi")]:
            p = d[col].values
            ok = ~np.isnan(p)
            rows[name] = np.mean((p[ok] - d.y.values[ok]) ** 2)
        best = min(rows, key=rows.get)
        print(f"{ph:6s} n={len(d):7d}  " +
              "  ".join(f"{k} {v:.5f}" for k, v in rows.items()) +
              f"  -> {best}")


def divergence_ev(f, label, B=300):
    f = f[f.fv_g.notna() & f.mid.notna()].copy()
    f["d"] = f.fv_g - f.mid
    print(f"\n=== DIVERGENCE EV {label} (taker at touch, net fee) ===")
    print("buy Up when d>+thr at ask; buy Down when d<-thr at 1-bid")
    out = []
    for lo, hi in [(0.02, 0.04), (0.04, 0.07), (0.07, 0.12), (0.12, 1.0)]:
        for sign in (+1, -1):
            if sign > 0:
                s = f[(f.d >= lo) & (f.d < hi)]
                entry = s.ask_tape.values
                win = s.y.values
            else:
                s = f[(f.d <= -lo) & (f.d > -hi)]
                entry = 1 - s.bid_tape.values
                win = 1 - s.y.values
            if len(s) < 200:
                continue
            fee = FEE * entry * (1 - entry)
            pnl = win - entry - fee
            # cluster bootstrap by market on per-market mean pnl
            pm = pd.DataFrame({"slug": s.slug.values, "pnl": pnl}).groupby("slug").pnl.mean()
            rng = np.random.default_rng(5)
            bs = [np.mean(rng.choice(pm.values, len(pm))) for _ in range(B)]
            lo_ci, hi_ci = np.quantile(bs, [0.025, 0.975])
            out.append((f"{'+' if sign>0 else '-'}[{lo},{hi})", len(s),
                        s.slug.nunique(), np.mean(entry), np.mean(pnl) * 100,
                        lo_ci * 100, hi_ci * 100))
    print(pd.DataFrame(out, columns=["d-bucket", "n", "mkts", "avg_entry",
                                     "EV c/sh", "ci_lo", "ci_hi"])
          .round(3).to_string(index=False))


def reconcile(f, label):
    f = f[f.fv_g.notna() & f.mid.notna()].copy()
    print(f"\n=== (mid, z) RECONCILIATION {label}: mean(y - mid) ===")
    tab = f.pivot_table(index=pd.cut(f.mid, [0, 0.2, 0.4, 0.6, 0.8, 1.0]),
                        columns=pd.cut(f.z, [-9, -2, -1, -0.3, 0.3, 1, 2, 9]),
                        values="y", aggfunc="mean", observed=True)
    mid_tab = f.pivot_table(index=pd.cut(f.mid, [0, 0.2, 0.4, 0.6, 0.8, 1.0]),
                            columns=pd.cut(f.z, [-9, -2, -1, -0.3, 0.3, 1, 2, 9]),
                            values="mid", aggfunc="mean", observed=True)
    n_tab = f.pivot_table(index=pd.cut(f.mid, [0, 0.2, 0.4, 0.6, 0.8, 1.0]),
                          columns=pd.cut(f.z, [-9, -2, -1, -0.3, 0.3, 1, 2, 9]),
                          values="y", aggfunc="size", observed=True)
    print("excess win rate (y - mid), cells with n>=500 shown:")
    print(((tab - mid_tab).where(n_tab >= 500) * 100).round(1).to_string())


if __name__ == "__main__":
    horizon = sys.argv[1]
    use_test = "test" in sys.argv[2:]
    g = load(horizon)
    part = g[~g.train] if use_test else g[g.train]
    tag = f"{horizon} {'TEST' if use_test else 'TRAIN'}"
    fresh = part[part.fresh]
    brier_race(fresh, tag)
    divergence_ev(fresh, tag)
    reconcile(fresh, tag)
