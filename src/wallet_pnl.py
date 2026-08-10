"""Who actually makes money here? Wallet-level attribution on the free tape.

The public data-api trade feed carries the taker's proxyWallet on every
print. That lets us do something the paper (and every price-only study)
cannot: measure realised P&L per participant, find the wallets with a
persistent edge, and then ask the only question that matters for us —

    does an informed wallet's print predict the outcome strongly enough,
    at a price we could still get, to be copyable after fees?

P&L convention (taker's perspective, per share):
    BUY  token X at p  ->  1{X wins} - p
    SELL token X at p  ->  p - 1{X wins}
Fees: takers pay 0.07*p*(1-p) per share on this venue.
"""
import argparse
import glob

import numpy as np
import pandas as pd

FEE = 0.07


def load(coins, root="data/pmfree", cap=None):
    metas, tapes = [], []
    for c in coins:
        mf = sorted(glob.glob(f"data/pmfree/meta/{c}_5m_*.parquet"))
        metas.append(pd.concat([pd.read_parquet(f) for f in mf],
                               ignore_index=True))
        tf = sorted(glob.glob(f"{root}/trades/{c}_5m_*.parquet"))
        if cap:
            tf = tf[:cap]
        for f in tf:
            d = pd.read_parquet(f)
            if len(d):
                d["coin"] = c
                tapes.append(d)
    meta = pd.concat(metas, ignore_index=True).drop_duplicates("slug")
    meta = meta[["slug", "t0", "t1", "up_win", "volume"]]
    t = pd.concat(tapes, ignore_index=True)
    t = t.merge(meta, on="slug", how="inner")
    t["rel"] = t.ts - t.t0
    t = t[(t.rel >= -60) & (t.rel <= 330)]
    tok_wins = np.where(t.is_up_tok, t.up_win, ~t.up_win)
    sgn = np.where(t.side == "BUY", 1.0, -1.0)
    t["edge"] = sgn * (tok_wins.astype(float) - t.price)      # gross per share
    t["fee"] = FEE * t.price * (1 - t.price)
    t["net"] = t.edge - t.fee
    t["day"] = pd.to_datetime(t.t0, unit="s", utc=True).dt.strftime("%Y-%m-%d")
    return t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", nargs="+", default=["eth", "sol", "xrp", "doge"])
    ap.add_argument("--root", default="data/pmfree")
    ap.add_argument("--min-shares", type=float, default=2000)
    a = ap.parse_args()

    t = load(a.coins, a.root)
    print(f"prints={len(t):,}  wallets={t.wallet.nunique():,}  "
          f"markets={t.slug.nunique():,}  "
          f"days={t.day.nunique()}  coins={sorted(t.coin.unique())}")
    print(f"\naggregate taker economics (every print, taker side):")
    print(f"  gross edge {t.edge.mean()*100:+.2f}c/share   "
          f"fee {-t.fee.mean()*100:+.2f}c   net {t.net.mean()*100:+.2f}c")
    print(f"  size-weighted net "
          f"{(t.net*t['size']).sum()/t['size'].sum()*100:+.2f}c/share  "
          f"total taker P&L ${(t.net*t['size']).sum():,.0f}")

    g = t.groupby("wallet").agg(
        sh=("size", "sum"), n=("size", "size"), mkts=("slug", "nunique"),
        gross=("edge", lambda x: np.nan), )
    g["pnl"] = t.assign(v=t.net * t["size"]).groupby("wallet")["v"].sum()
    g["gross_pnl"] = t.assign(v=t.edge * t["size"]).groupby("wallet")["v"].sum()
    g["c_per_sh"] = g.pnl / g.sh * 100
    g = g.drop(columns=["gross"])
    big = g[g.sh >= a.min_shares].sort_values("pnl", ascending=False)
    print(f"\nwallets with >= {a.min_shares:,.0f} shares traded: {len(big):,}"
          f"  (of {len(g):,})")
    print(f"  they are {big.sh.sum()/g.sh.sum():.0%} of all taker volume")
    pos = (big.pnl > 0).mean()
    print(f"  {pos:.0%} of them are net-positive after fees")

    pd.set_option("display.width", 200)
    print("\n--- TOP 15 taker wallets by net P&L ---")
    print(big.head(15).to_string(
        float_format=lambda v: f"{v:,.1f}"))
    print("\n--- BOTTOM 10 ---")
    print(big.tail(10).to_string(float_format=lambda v: f"{v:,.1f}"))

    # concentration
    tot = big.pnl.clip(lower=0).sum()
    top = big.pnl.head(10).sum()
    print(f"\ntop-10 wallets capture ${top:,.0f} of ${tot:,.0f} "
          f"of all positive taker P&L ({top/max(tot,1):.0%})")

    # when do the winners trade?
    win = set(big[(big.pnl > 0) & (big.c_per_sh > 1)].index)
    lose = set(big[(big.pnl < 0)].index)
    for lbl, ws in (("winners", win), ("losers", lose)):
        s = t[t.wallet.isin(ws)]
        if not len(s):
            continue
        print(f"\n{lbl}: {len(ws)} wallets, {len(s):,} prints, "
              f"{s['size'].sum():,.0f} shares, "
              f"net {(s.net*s['size']).sum()/s['size'].sum()*100:+.2f}c/share")
        q = s.groupby(pd.cut(s.rel, [-60, 0, 60, 120, 180, 240, 270, 300, 330]))
        r = q.apply(lambda x: pd.Series({
            "prints": len(x), "shares": x['size'].sum(),
            "c_per_sh": (x.net * x['size']).sum() / max(x['size'].sum(), 1) * 100,
            "avg_px": (x.price * x['size']).sum() / max(x['size'].sum(), 1)}))
        print(r.to_string(float_format=lambda v: f"{v:,.2f}"))


if __name__ == "__main__":
    main()
