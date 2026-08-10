"""Does the book price the NEW (trailing-TWAP) contract in its final seconds?

Verified rule (post 2026-08-07):
    Up  iff  mean(P over [t1-30, t1))  >=  mean(P over [t0-30, t0))

Inside the last 30 seconds the outcome is progressively LOCKED: with the
first n seconds of the settle window already printed, only the remaining
(30-n) seconds can still move the average, and their influence on the mean
decays as rem/30. The old contract had no such lock-in — at t1-1s it was
still a live coin flip whenever spot sat near the strike.

So a book still quoting the old contract must be systematically wrong late
in the window, in a direction we can compute.

For every print at time tau in [t1-30, t1):
    K    = strike           = mean(P over [t0-30, t0))       (known at t0)
    S    = sum(P over [t1-30, tau))                          (locked)
    rem  = t1 - tau                                          (still to come)
    M    = S + rem*spot - 30*K        expected margin, price-seconds
    sd   = sigma * sqrt(rem^3 / 3)    sd of the remaining contribution
    z    = M / sd
P(Up) is then a function of z alone; we do NOT assume it is Phi(z) — we
measure it from the settled outcomes and check calibration.

Execution is priced against real prints only:
    buy Up   at an ask-hit print price p     -> EV = 1{Up}   - p - fee(p)
    buy Down at 1 - a bid-hit print price b  -> EV = 1{Down} - (1-b) - fee
with the venue's verified taker fee 0.07*p*(1-p) and no fill assumptions.
"""
import argparse
import glob

import numpy as np
import pandas as pd

from rollavg_edge_test import load_1s, CHANGE

FEE = 0.07
W = 30


def build(coin, root, lo_rel, days_filter=None, era="post"):
    mf = sorted(glob.glob(f"data/pmfree/meta/{coin}_5m_*.parquet"))
    meta = pd.concat([pd.read_parquet(f) for f in mf], ignore_index=True)
    meta = meta.drop_duplicates("slug")
    tf = sorted(glob.glob(f"{root}/trades/{coin}_5m_*.parquet"))
    if days_filter:
        tf = [f for f in tf if any(d in f for d in days_filter)]
    if not tf:
        return None
    t = pd.concat([pd.read_parquet(f) for f in tf], ignore_index=True)
    t = t.merge(meta[["slug", "t0", "t1", "up_win"]], on="slug", how="inner")
    t = t[(t.t0 >= CHANGE) if era == "post" else (t.t0 < CHANGE)]
    t["rel"] = t.ts - t.t0
    t = t[(t.rel >= lo_rel) & (t.rel < 300)].copy()
    if not len(t):
        return None
    days = sorted(pd.to_datetime(t.t0, unit="s", utc=True)
                  .dt.strftime("%Y-%m-%d").unique())
    px = load_1s(coin, days)
    if px is None:
        return None
    # STRICT CAUSALITY: binance 1s klines are indexed by OPEN time, so the
    # bar at second s covers [s, s+1) and its close is only known at s+1.
    # Re-index by close time so every lookup at tau uses data available at
    # tau. Without this the model peeks one second into the future, which
    # at rem<30s is a large part of the remaining uncertainty.
    px = px.copy()
    px.index = px.index + 1
    lo = px.index[0]
    v = px.values.astype("float64")
    cs = np.concatenate([[0.0], np.cumsum(v)])

    def csum(a, b):
        ia = np.clip(a - lo, 0, len(v)); ib = np.clip(b - lo, 0, len(v))
        return cs[ib] - cs[ia], ib - ia

    def spot_at(x):
        i = np.clip(x - lo, 0, len(v) - 1)
        return np.where((x - lo >= 0) & (x - lo < len(v)), v[i], np.nan)

    ks, kn = csum(t.t0.values - W, t.t0.values)
    t["K"] = np.where(kn >= W - 2, ks / np.maximum(kn, 1), np.nan)
    ss, sn = csum(t.t1.values - W, t.ts.values)
    t["S"] = np.where(sn >= 0, ss, np.nan)
    t["nlock"] = sn
    t["spot"] = spot_at(t.ts.values)
    t["rem"] = t.t1.values - t.ts.values

    # sigma: per-second dollar vol from the trailing hour of 1s closes
    r = np.log(px / px.shift(1))
    sig = (r.rolling(3600, min_periods=600).std()
           .reindex(t.ts.values).values) * t["spot"].values
    t["sigma"] = sig
    t["M"] = t.S + t.rem * t.spot - W * t.K
    t["sd"] = t.sigma * np.sqrt(np.maximum(t.rem ** 3 / 3.0, 1e-12))
    t["z"] = t.M / t.sd
    t["is_ask"] = ((t.is_up_tok) & (t.side == "BUY")) | \
                  ((~t.is_up_tok) & (t.side == "SELL"))
    t["date"] = pd.to_datetime(t.t0, unit="s", utc=True).dt.strftime("%Y-%m-%d")
    t["coin"] = coin
    return t.dropna(subset=["z", "K", "spot", "sigma"])


def calib(t):
    print(f"\n  calibration of z -> P(Up)   (n={len(t):,} prints, "
          f"{t.slug.nunique():,} markets)")
    b = pd.cut(t.z, [-99, -3, -2, -1, -.5, 0, .5, 1, 2, 3, 99])
    g = t.groupby(b, observed=True).agg(
        prints=("z", "size"), mkts=("slug", "nunique"),
        P_up=("up_win", "mean"), mkt_px=("p_up", "mean"))
    g["gap_pp"] = (g.P_up - g.mkt_px) * 100
    print(g.to_string(float_format=lambda v: f"{v:,.4f}"))


def trade(t, zmin, label):
    """Take the side z favours, at real print prices, net of the real fee."""
    up = t[(t.z >= zmin) & t.is_ask]              # buy Up at an ask
    dn = t[(t.z <= -zmin) & (~t.is_ask)]          # buy Down at 1-bid
    rows = []
    if len(up):
        p = up.p_up.values
        rows.append(pd.DataFrame({
            "px": p, "win": up.up_win.values.astype(float),
            "size": up["size"].values, "date": up.date.values,
            "coin": up.coin.values, "side": "Up"}))
    if len(dn):
        p = 1 - dn.p_up.values
        rows.append(pd.DataFrame({
            "px": p, "win": (~dn.up_win.values).astype(float),
            "size": dn["size"].values, "date": dn.date.values,
            "coin": dn.coin.values, "side": "Dn"}))
    if not rows:
        print(f"  {label}: no trades")
        return None
    d = pd.concat(rows, ignore_index=True)
    d["ev"] = d.win - d.px - FEE * d.px * (1 - d.px)
    g = d.groupby("date").apply(
        lambda x: pd.Series({"n": len(x), "sh": x["size"].sum(),
                             "ev": (x.ev * x["size"]).sum() / x["size"].sum()}))
    m = (d.ev * d["size"]).sum() / d["size"].sum()
    se = g.ev.std(ddof=1) / np.sqrt(len(g)) if len(g) > 2 else np.nan
    print(f"  {label:<26} n={len(d):>7,} shares={d['size'].sum():>11,.0f} "
          f"avg_px={d.px.mean():.4f} hit={d.win.mean():.4f} "
          f"EV={m*100:+.2f}c/sh  (per-day SE {se*100 if se==se else float('nan'):.2f}, "
          f"{len(g)} days)")
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", nargs="+", default=["btc"])
    ap.add_argument("--root", default="data/pmfree_aug")
    ap.add_argument("--lo-rel", type=int, default=240)
    ap.add_argument("--era", default="post", choices=["post", "pre"])
    a = ap.parse_args()
    parts = []
    for c in a.coins:
        d = build(c, a.root, a.lo_rel, era=a.era)
        if d is not None:
            parts.append(d)
            print(f"{c}: {len(d):,} {a.era}-change prints in last "
                  f"{300-a.lo_rel}s, {d.slug.nunique():,} markets, "
                  f"{sorted(d.date.unique())}")
    if not parts:
        print("no post-change tape yet")
        return
    t = pd.concat(parts, ignore_index=True)
    calib(t)
    print("\n  taker EV against real prints, by |z| gate "
          "(last %ds of the window):" % (300 - a.lo_rel))
    for zmin in (0.5, 1.0, 1.5, 2.0, 3.0):
        trade(t, zmin, f"|z|>={zmin}")
    by_day_coin(t, 1.0)
    print("\n  restricted to the final 15s:")
    t15 = t[t.rel >= 285]
    for zmin in (1.0, 2.0, 3.0):
        trade(t15, zmin, f"final15 |z|>={zmin}")




def by_day_coin(t, zmin=1.0):
    """Decay and cross-coin replication: the same gate, split every way."""
    up = t[(t.z >= zmin) & t.is_ask]
    dn = t[(t.z <= -zmin) & (~t.is_ask)]
    d = pd.concat([
        pd.DataFrame({"px": up.p_up.values, "win": up.up_win.values.astype(float),
                      "size": up["size"].values, "date": up.date.values,
                      "coin": up.coin.values, "rel": up.rel.values}),
        pd.DataFrame({"px": 1 - dn.p_up.values,
                      "win": (~dn.up_win.values).astype(float),
                      "size": dn["size"].values, "date": dn.date.values,
                      "coin": dn.coin.values, "rel": dn.rel.values})],
        ignore_index=True)
    d["ev"] = d.win - d.px - FEE * d.px * (1 - d.px)

    def agg(x):
        return pd.Series({
            "prints": len(x), "shares": x["size"].sum(),
            "avg_px": (x.px * x["size"]).sum() / x["size"].sum(),
            "hit": (x.win * x["size"]).sum() / x["size"].sum(),
            "EV_c": (x.ev * x["size"]).sum() / x["size"].sum() * 100,
            "gross$": (x.ev * x["size"]).sum()})
    print(f"\n  |z|>={zmin} — by DAY")
    print(d.groupby("date").apply(agg).to_string(
        float_format=lambda v: f"{v:,.2f}"))
    print(f"\n  |z|>={zmin} — by COIN")
    print(d.groupby("coin").apply(agg).to_string(
        float_format=lambda v: f"{v:,.2f}"))
    print(f"\n  |z|>={zmin} — by COIN x DAY (EV c/share)")
    print(d.pivot_table(index="coin", columns="date", values="ev",
                        aggfunc=lambda x: x.mean() * 100).to_string(
        float_format=lambda v: f"{v:+.2f}"))
    return d


if __name__ == "__main__":
    main()
