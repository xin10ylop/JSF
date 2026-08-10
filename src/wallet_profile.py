"""What do the persistently-profitable wallets actually DO?

Copying their direction fails, so their edge must live in price selection,
timing, or the side of the book they are on. This profiles the cohort that
was selected on a training slice and stayed profitable out of sample, and
contrasts it with the losing cohort on the same axes.
"""
import argparse

import numpy as np
import pandas as pd

from wallet_oos import load, wsum, FEE


def prof(t, ws, label):
    s = t[t.wallet.isin(ws)]
    if not len(s):
        print(f"{label}: none")
        return
    print(f"\n=== {label}: {len(ws)} wallets, {len(s):,} prints, "
          f"{s['size'].sum():,.0f} shares, net {wsum(s,'net')*100:+.2f}c/sh ===")
    print(f"  median print size {s['size'].median():.0f}  "
          f"mean {s['size'].mean():.0f}  "
          f"prints/market {len(s)/s.slug.nunique():.1f}  "
          f"markets {s.slug.nunique():,}")
    b = s[s.side == "BUY"]; sl = s[s.side == "SELL"]
    print(f"  BUY {len(b)/len(s):.0%} of prints @ avg px "
          f"{wsum(b,'price') if len(b) else float('nan'):.3f} "
          f"net {wsum(b,'net')*100 if len(b) else float('nan'):+.2f}c   |   "
          f"SELL {len(sl)/len(s):.0%} @ {wsum(sl,'price') if len(sl) else float('nan'):.3f} "
          f"net {wsum(sl,'net')*100 if len(sl) else float('nan'):+.2f}c")
    q = pd.cut(s.price, [0, .05, .15, .35, .65, .85, .95, 1.0])
    r = s.groupby(q, observed=True).apply(lambda x: pd.Series({
        "prints": len(x), "shares": x["size"].sum(),
        "share_pct": x["size"].sum() / s["size"].sum() * 100,
        "gross_c": wsum(x, "gross") * 100,
        "fee_c": -FEE * (x.price * (1 - x.price) * x["size"]).sum()
                 / x["size"].sum() * 100,
        "net_c": wsum(x, "net") * 100}))
    print(r.to_string(float_format=lambda v: f"{v:,.2f}"))
    q2 = pd.cut(s.rel, [-60, 0, 60, 120, 180, 240, 285, 300, 330])
    r2 = s.groupby(q2, observed=True).apply(lambda x: pd.Series({
        "shares": x["size"].sum(),
        "share_pct": x["size"].sum() / s["size"].sum() * 100,
        "avg_px": wsum(x, "price"),
        "net_c": wsum(x, "net") * 100}))
    print(r2.to_string(float_format=lambda v: f"{v:,.2f}"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", nargs="+", default=["doge"])
    ap.add_argument("--root", default="data/pmfree")
    ap.add_argument("--min-shares", type=float, default=1000)
    a = ap.parse_args()
    t = load(a.coins, a.root)
    days = sorted(t.date.unique())
    split = days[int(len(days) * 0.6)]
    tr, te = t[t.date < split], t[t.date >= split]

    g = tr.groupby("wallet").agg(sh=("size", "sum"))
    g["c"] = tr.assign(v=tr.net * tr["size"]).groupby("wallet")["v"].sum() \
        / g.sh * 100
    g = g[g.sh >= a.min_shares]
    gt = te.groupby("wallet").agg(sh=("size", "sum"))
    j = g.join(gt, rsuffix="_te", how="inner")
    j = j[j.sh_te >= 200]
    k = max(len(j) // 10, 3)
    smart = set(j.nlargest(k, "c").index)
    dumb = set(j.nsmallest(k, "c").index)
    print(f"selected on {split[:10]}-split: {len(smart)} smart / {len(dumb)} "
          f"dumb wallets; profiling on TEST slice only")
    prof(te, smart, "SMART (train-selected)")
    prof(te, dumb, "DUMB (train-selected)")
    prof(te, set(te.wallet.unique()) - smart - dumb, "EVERYONE ELSE")


if __name__ == "__main__":
    main()
