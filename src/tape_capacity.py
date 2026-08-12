"""How much of the endgame edge is actually TAKEABLE?

The live paper bot fills against the displayed book: it sees an ask of N
shares at p and assumes it wins the race for all N, every time. Nothing in
that model knows whether a trade at p ever happened, or whether anyone was
willing to sell N shares there.

This script asks the opposite question, using only the venue's own trade
tape: for every real print that our signal would have wanted to hit, how
many shares actually changed hands? A print is proof that (a) the price was
reachable and (b) somebody supplied that size. Capping our fill at a
fraction of realised volume is the standard participation-rate constraint,
and it answers three of the realism gaps at once:

  * we can only take what someone actually sold,
  * we cannot take more than the market absorbed without moving it,
  * we displace at most `--alpha` of the real counterparty's trade.

Everything here is measurement, not simulation. Output is shares/day and
dollars/day at the alpha you choose.

    python3 src/tape_capacity.py --coin btc --fam 5m --alpha 0.25
"""
import argparse
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "src")
from endgame_rollavg import build  # noqa: E402

FEE = 0.07


def qualify(t, zmin, max_price, lo_rem, hi_rem):
    """Rows the endgame taker would want to hit, with cost and outcome.

    Up  is bought at an ask print   (is_ask)      paying p_up.
    Down is bought at a bid print   (~is_ask)     paying 1 - p_up.
    """
    t = t[(t.rem > lo_rem) & (t.rem <= hi_rem)].copy()
    up = t[(t.z >= zmin) & (t.is_ask)].copy()
    up["px"] = up.p_up
    up["won"] = up.up_win.astype(float)
    up["side"] = "Up"
    dn = t[(t.z <= -zmin) & (~t.is_ask)].copy()
    dn["px"] = 1.0 - dn.p_up
    dn["won"] = 1.0 - dn.up_win.astype(float)
    dn["side"] = "Down"
    q = pd.concat([up, dn], ignore_index=True)
    q = q[q.px <= max_price]
    q["fee"] = FEE * q.px * (1 - q.px)
    q["pnl_per_sh"] = q.won - q.px - q.fee
    return q


def clustered_t(pnl, size, cluster):
    """t-stat of mean P&L per share with errors clustered by market.

    Fills inside one market share a single outcome: 40 fills on the same
    slug are one bet, not 40. Treating them as independent inflates t by
    roughly sqrt(fills per market) -- a factor of 3-4 here.
    """
    df = pd.DataFrame({"pnl": pnl * size, "sz": size, "g": cluster})
    g = df.groupby("g").sum()
    if len(g) < 3:
        return np.nan, len(g)
    tot = g.sz.sum()
    mu = g.pnl.sum() / tot
    # per-market residual of total P&L against the shares it traded
    resid = g.pnl - mu * g.sz
    se = np.sqrt((resid ** 2).sum()) / tot
    return (mu / se if se > 0 else np.nan), len(g)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", default="btc")
    ap.add_argument("--root", default="data/pmfree_aug")
    ap.add_argument("--zmin", type=float, default=2.0)
    ap.add_argument("--max-price", type=float, default=0.99)
    ap.add_argument("--lo-rem", type=float, default=2.0)
    # 5m markets are tradable only in their last 30s (bot/strategy.py
    # gates on rem > min(window_s, m.w), m.w = 30 for 5m). Defaulting to 60
    # here mixed in prints the bot cannot reach.
    ap.add_argument("--hi-rem", type=float, default=30.0)
    ap.add_argument("--alpha", type=float, default=0.25,
                    help="share of each real print we assume we could take")
    ap.add_argument("--era", default="post")
    a = ap.parse_args()

    t = build(a.coin, a.root, lo_rel=0, era=a.era)
    if t is None or not len(t):
        print("no data")
        return
    q = qualify(t, a.zmin, a.max_price, a.lo_rem, a.hi_rem)
    if not len(q):
        print("no qualifying prints")
        return
    days = q.date.nunique()
    sh = q["size"].values
    pps = q.pnl_per_sh.values

    print(f"=== {a.coin.upper()} {a.era}-change, rem in "
          f"({a.lo_rem:g}, {a.hi_rem:g}]s, |z|>={a.zmin}, px<={a.max_price} ===")
    print(f"days {days}   markets {q.slug.nunique():,}   "
          f"qualifying prints {len(q):,}")
    print(f"TOTAL printed size {sh.sum():,.0f} shares "
          f"(${(sh * q.px).sum():,.0f} notional)")
    tstat, ng = clustered_t(pps, sh, q.slug.values)
    print(f"edge {np.average(pps, weights=sh) * 100:+.2f}c/share   "
          f"hit {np.average(q.won, weights=sh):.3f}   "
          f"t(day-clustered by market, n={ng}) = {tstat:+.2f}")

    print(f"\n--- at alpha={a.alpha:g} (we take that share of each print) ---")
    got = sh * a.alpha
    pnl = (got * pps).sum()
    print(f"  {got.sum():,.0f} shares  ${pnl:+,.2f} total  "
          f"= ${pnl / days:+,.2f}/day on {got.sum() / days:,.0f} shares/day")

    print("\nby fill price:")
    b = pd.cut(q.px, [0, .3, .5, .7, .85, .92, .95, .98, 1.0])
    tab = q.groupby(b, observed=True).apply(lambda x: pd.Series({
        "prints": len(x),
        "shares": x["size"].sum(),
        "sh/day": x["size"].sum() / days,
        "hit": np.average(x.won, weights=x["size"]),
        "c/sh": np.average(x.pnl_per_sh, weights=x["size"]) * 100,
        "$/day@a": a.alpha * (x["size"] * x.pnl_per_sh).sum() / days,
    }), include_groups=False)
    print(tab.to_string(float_format=lambda v: f"{v:,.2f}"))

    print("\nper-print size distribution (what one counterparty supplies):")
    print(pd.Series(sh).describe(
        percentiles=[.25, .5, .75, .9, .99]).to_string(
            float_format=lambda v: f"{v:,.1f}"))

    per_mkt = q.groupby("slug")["size"].sum() * a.alpha
    print(f"\nshares/market at alpha: mean {per_mkt.mean():,.0f}  "
          f"median {per_mkt.median():,.0f}  max {per_mkt.max():,.0f}")
    print(f"markets with a signal: {len(per_mkt):,} of "
          f"{t.slug.nunique():,} ({len(per_mkt) / t.slug.nunique():.0%})")


if __name__ == "__main__":
    main()
