"""The endgame edge, decided LATE and filled at real prints.

Two things the paper broker gets for free and shouldn't:

  * it decides on the same instant it fills. Polymarket holds taker orders
    on crypto up/down markets for 250 ms before matching, and the network
    round trip adds more, so the decision is made several hundred
    milliseconds before the fill.
  * it fills against displayed depth. A displayed ask is an offer; a print
    is a completed trade. Only prints prove the price and the size existed.

Here the signal is computed from information available `--lag` seconds
before the print, and the fill happens at the print. The tape's timestamps
are whole seconds, so lag=1 is the smallest honest step and is already
about 2.5x the real 400 ms delay -- a conservative bound, not a tuned one.

    python3 src/tape_latency.py --lag 1
"""
import argparse
import glob
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "src")
from rollavg_edge_test import load_1s, CHANGE  # noqa: E402
from tape_capacity import clustered_t  # noqa: E402

FEE = 0.07


def build_lagged(coin, root, fam, lag, era="post"):
    """Same contract algebra as endgame_rollavg.build, but z is evaluated
    at ts-lag while the fill still happens at the print at ts."""
    w = 30 if fam == "5m" else 60
    dur = 300 if fam == "5m" else 900
    mf = sorted(glob.glob(f"data/pmfree/meta/{coin}_{fam}_*.parquet"))
    if not mf:
        return None
    meta = pd.concat([pd.read_parquet(f) for f in mf],
                     ignore_index=True).drop_duplicates("slug")
    tf = sorted(glob.glob(f"{root}/trades/{coin}_{fam}_*.parquet"))
    if not tf:
        return None
    t = pd.concat([pd.read_parquet(f) for f in tf], ignore_index=True)
    t = t.merge(meta[["slug", "t0", "t1", "up_win"]], on="slug", how="inner")
    t = t[(t.t0 >= CHANGE) if era == "post" else (t.t0 < CHANGE)]
    t["rel"] = t.ts - t.t0
    t = t[(t.rel >= 0) & (t.rel < dur)].copy()
    if not len(t):
        return None
    days = sorted(pd.to_datetime(t.t0, unit="s", utc=True)
                  .dt.strftime("%Y-%m-%d").unique())
    px = load_1s(coin, days)
    if px is None:
        return None
    px = px.copy()
    px.index = px.index + 1          # klines are stamped by OPEN time
    lo = px.index[0]
    v = px.values.astype("float64")
    cs = np.concatenate([[0.0], np.cumsum(v)])

    def csum(a, b):
        ia = np.clip(a - lo, 0, len(v)); ib = np.clip(b - lo, 0, len(v))
        return cs[ib] - cs[ia], ib - ia

    def spot_at(x):
        i = np.clip(x - lo, 0, len(v) - 1)
        return np.where((x - lo >= 0) & (x - lo < len(v)), v[i], np.nan)

    # the decision instant: `lag` seconds before the print we intend to hit
    te = t.ts.values - lag
    ks, kn = csum(t.t0.values - w, t.t0.values)
    t["K"] = np.where(kn >= w - 2, ks / np.maximum(kn, 1), np.nan)
    ss, sn = csum(t.t1.values - w, te)
    inside = sn >= 0
    t["S"] = np.where(inside, ss, 0.0)
    t["spot"] = spot_at(te)
    t["rem_e"] = t.t1.values - te            # remaining AT THE DECISION
    t["rem"] = t.t1.values - t.ts.values     # remaining at the fill
    r = np.log(px / px.shift(1))
    t["sigma"] = (r.rolling(3600, min_periods=600).std()
                  .reindex(te).values) * t["spot"].values
    s_pre = np.maximum((t.t1.values - w) - te, 0.0)
    M = np.where(inside, t.S + t.rem_e * t.spot - w * t.K, t.spot - t.K)
    sd = np.where(inside,
                  t.sigma * np.sqrt(np.maximum(t.rem_e ** 3 / 3.0, 1e-12)),
                  t.sigma * np.sqrt(np.maximum(s_pre + w / 3.0, 1e-12)))
    t["z"] = M / sd
    t["is_ask"] = ((t.is_up_tok) & (t.side == "BUY")) | \
                  ((~t.is_up_tok) & (t.side == "SELL"))
    t["date"] = pd.to_datetime(t.t0, unit="s", utc=True).dt.strftime("%Y-%m-%d")
    return t.dropna(subset=["z", "K", "spot", "sigma"])


def run(t, zmin, max_price, lo_rem, hi_rem, cap, label, pick="first"):
    t = t[(t.rem > lo_rem) & (t.rem <= hi_rem)]
    up = t[(t.z >= zmin) & (t.is_ask)].copy()
    up["px"] = up.p_up
    up["won"] = up.up_win.astype(float)
    dn = t[(t.z <= -zmin) & (~t.is_ask)].copy()
    dn["px"] = 1.0 - dn.p_up
    dn["won"] = 1.0 - dn.up_win.astype(float)
    q = pd.concat([up, dn], ignore_index=True)
    q = q[q.px <= max_price]
    if not len(q):
        print(f"{label:>18}  no qualifying prints")
        return
    q["pnl_per_sh"] = q.won - q.px - FEE * q.px * (1 - q.px)
    # Which prints do we win? `first` assumes we beat everyone to the earliest
    # signal in each market -- the most flattering assumption, since those are
    # the ones every other fast participant also wants. `last` is the opposite
    # extreme. `uniform` spreads our cap evenly over the market's qualifying
    # flow, which is what a constant-participation taker actually gets.
    if pick == "uniform":
        tot = q.groupby("slug")["size"].transform("sum")
        q["qty"] = q["size"] * np.minimum(cap / tot.clip(lower=1e-9), 1.0)
    else:
        q = q.sort_values(["slug", "rem"],
                          ascending=[True, pick == "last"])
        q["cum"] = q.groupby("slug")["size"].cumsum()
        q["qty"] = np.clip(cap - (q.cum - q["size"]), 0, q["size"])
    k = q[q.qty > 0]
    days = k.date.nunique()
    pnl = (k.qty * k.pnl_per_sh).sum()
    tt, ng = clustered_t(k.pnl_per_sh.values, k.qty.values, k.slug.values)
    print(f"{label:>18}  {k.qty.sum() / days:>8,.0f} sh/day  "
          f"{100 * np.average(k.pnl_per_sh, weights=k.qty):>+6.2f}c/sh  "
          f"hit {np.average(k.won, weights=k.qty):.3f}  "
          f"${pnl / days:>+8,.0f}/day  t={tt:>+5.2f} (n={ng})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", default="btc")
    ap.add_argument("--fams", default="5m")
    ap.add_argument("--root", default="data/pmfree_aug")
    ap.add_argument("--zmin", type=float, default=2.0)
    ap.add_argument("--max-price", type=float, default=0.99)
    ap.add_argument("--lo-rem", type=float, default=2.0)
    ap.add_argument("--hi-rem", type=float, default=60.0)
    ap.add_argument("--cap", type=float, default=200.0,
                    help="max shares per market (our participation limit)")
    ap.add_argument("--lags", default="0,1,2,3")
    ap.add_argument("--picks", default="first",
                    help="first|last|uniform, comma separated")
    a = ap.parse_args()
    for coin in a.coins.split(","):
        for fam in a.fams.split(","):
            print(f"\n### {coin.upper()} {fam}  cap={a.cap:g} sh/market  "
                  f"|z|>={a.zmin}  px<={a.max_price}  rem "
                  f"({a.lo_rem:g},{a.hi_rem:g}]s")
            for lag in [int(x) for x in a.lags.split(",")]:
                t = build_lagged(coin, a.root, fam, lag)
                if t is None or not len(t):
                    print(f"  lag={lag}s: no data")
                    continue
                for pick in a.picks.split(","):
                    run(t, a.zmin, a.max_price, a.lo_rem, a.hi_rem, a.cap,
                        f"lag {lag}s / {pick}", pick)


if __name__ == "__main__":
    main()
