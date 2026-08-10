"""Validate the 15-minute family the bot also trades.

The headline edge (+3.04c pooled, +7.37c at the bot's ask<=0.97 gate) was
measured on 5m markets only -- `endgame_rollavg.build()` globs `*_5m_*`.
Live paper fills showed 44% of traded shares landing in 15m markets, so the
bot was trading a family that had never been measured. This closes that.

15m markets settle on a 60-SECOND trailing TWAP, confirmed two ways: their
resolutionSource is `<coin>-usd-twap-60s-streams`, and an accuracy scan over
w peaks at exactly 60s (0.9744) versus 0.9617 at 30s.

Result (2026-08-07..10, btc/eth/sol):
    post-change  |z|>=2, ask<=0.97 -> +6.18c/share, hit 0.781, 52,916 shares
    pre-change control              -> -3.26c/share
i.e. the 15m contrast is sharper than 5m, whose control was +1.39c.
"""
import glob
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "src")
from rollavg_edge_test import load_1s, CHANGE  # noqa: E402

FEE = 0.07
W = 60
HORIZON_S = 900


def build15(coin, era, root="data/pmfree_15m"):
    mf = sorted(glob.glob(f"data/pmfree/meta/{coin}_15m_*.parquet"))
    if not mf:
        return None
    meta = pd.concat([pd.read_parquet(f) for f in mf],
                     ignore_index=True).drop_duplicates("slug")
    tf = sorted(glob.glob(f"{root}/trades/{coin}_15m_*.parquet"))
    if not tf:
        return None
    t = pd.concat([pd.read_parquet(f) for f in tf], ignore_index=True)
    t = t.merge(meta[["slug", "t0", "t1", "up_win"]], on="slug", how="inner")
    t = t[(t.t0 >= CHANGE) if era == "post" else (t.t0 < CHANGE)]
    t["rel"] = t.ts - t.t0
    t = t[(t.rel >= HORIZON_S - W) & (t.rel < HORIZON_S)].copy()
    if not len(t):
        return None
    days = sorted(pd.to_datetime(t.t0, unit="s", utc=True)
                  .dt.strftime("%Y-%m-%d").unique())
    px = load_1s(coin, days)
    if px is None:
        return None
    px = px.copy()
    px.index = px.index + 1                 # causal: index by close time
    lo = px.index[0]
    v = px.values.astype("float64")
    cs = np.concatenate([[0.0], np.cumsum(v)])

    def csum(a, b):
        ia = np.clip(a - lo, 0, len(v)); ib = np.clip(b - lo, 0, len(v))
        return cs[ib] - cs[ia], ib - ia

    def spot(x):
        i = np.clip(x - lo, 0, len(v) - 1)
        return np.where((x - lo >= 0) & (x - lo < len(v)), v[i], np.nan)

    ks, kn = csum(t.t0.values - W, t.t0.values)
    t["K"] = np.where(kn >= W - 2, ks / np.maximum(kn, 1), np.nan)
    ss, sn = csum(t.t1.values - W, t.ts.values)
    t["S"] = np.where(sn >= 0, ss, 0.0)
    t["spot"] = spot(t.ts.values)
    t["rem"] = t.t1.values - t.ts.values
    r = np.log(px / px.shift(1))
    t["sigma"] = (r.rolling(3600, min_periods=600).std()
                  .reindex(t.ts.values).values) * t["spot"].values
    t["z"] = ((t.S + t.rem * t.spot - W * t.K)
              / (t.sigma * np.sqrt(np.maximum(t.rem ** 3 / 3.0, 1e-12))))
    t["is_ask"] = ((t.is_up_tok) & (t.side == "BUY")) | \
                  ((~t.is_up_tok) & (t.side == "SELL"))
    t["date"] = pd.to_datetime(t.t0, unit="s", utc=True).dt.strftime("%Y-%m-%d")
    t["coin"] = coin
    return t.dropna(subset=["z", "K", "spot", "sigma"])


def main():
    coins = sys.argv[1:] or ["btc", "eth", "sol"]
    for era in ("post", "pre"):
        ps = [build15(c, era) for c in coins]
        ps = [p for p in ps if p is not None]
        if not ps:
            print(f"{era}: no data")
            continue
        t = pd.concat(ps, ignore_index=True)
        print(f"\n=== 15m {era.upper()}-change: {len(t):,} prints, "
              f"{t.slug.nunique():,} markets, {t.date.nunique()} days ===")
        for mp in (0.97, 0.99):
            up = t[(t.z >= 2.0) & t.is_ask].copy()
            up["px"] = up.p_up; up["win"] = up.up_win.astype(float)
            dn = t[(t.z <= -2.0) & (~t.is_ask)].copy()
            dn["px"] = 1 - dn.p_up; dn["win"] = (~dn.up_win).astype(float)
            d = pd.concat([up, dn], ignore_index=True)
            d = d[d.px <= mp]
            if not len(d):
                continue
            d["ev"] = d.win - d.px - FEE * d.px * (1 - d.px)
            w = d["size"]
            print(f"  |z|>=2 ask<={mp:.2f} | EV {(d.ev*w).sum()/w.sum()*100:+6.2f}c "
                  f" hit {(d.win*w).sum()/w.sum():.3f} "
                  f" avg_px {(d.px*w).sum()/w.sum():.4f} "
                  f" shares {w.sum():>9,.0f}  markets {d.slug.nunique():,}")


if __name__ == "__main__":
    main()
