"""Does wallet skill PERSIST out of sample, and is it followable?

Step 1  rank wallets on a training slice by net cents/share
Step 2  measure the SAME wallets on a disjoint later slice (no re-ranking)
Step 3  if skill persists, price the copy trade: after a smart wallet's
        print we would have to take the same side ourselves, paying the
        taker fee and whatever the price has moved to by the time we act.

The follow price is taken from the tape itself: the first print on the same
market and same direction occurring `lag` seconds or more after the signal
print. That is a price at which a trade demonstrably happened after we would
have decided, so it needs no fill assumption.
"""
import argparse
import glob

import numpy as np
import pandas as pd

FEE = 0.07


def load(coins, root="data/pmfree"):
    metas, tapes = [], []
    for c in coins:
        mf = sorted(glob.glob(f"data/pmfree/meta/{c}_5m_*.parquet"))
        metas.append(pd.concat([pd.read_parquet(f) for f in mf],
                               ignore_index=True))
        for f in sorted(glob.glob(f"{root}/trades/{c}_5m_*.parquet")):
            d = pd.read_parquet(f)
            if len(d):
                d["coin"] = c
                tapes.append(d)
    meta = pd.concat(metas, ignore_index=True).drop_duplicates("slug")
    t = pd.concat(tapes, ignore_index=True).merge(
        meta[["slug", "t0", "up_win"]], on="slug", how="inner")
    t["rel"] = t.ts - t.t0
    t = t[(t.rel >= -60) & (t.rel <= 330)].copy()
    tok_win = np.where(t.is_up_tok, t.up_win, ~t.up_win).astype(float)
    sgn = np.where(t.side == "BUY", 1.0, -1.0)
    t["gross"] = sgn * (tok_win - t.price)
    t["net"] = t.gross - FEE * t.price * (1 - t.price)
    t["date"] = pd.to_datetime(t.t0, unit="s", utc=True).dt.strftime("%Y-%m-%d")
    # direction in Up-token terms: +1 == a bet that Up wins
    t["dir_up"] = np.where(t.is_up_tok, sgn, -sgn)
    return t.sort_values(["slug", "ts"]).reset_index(drop=True)


def wsum(d, val, w="size"):
    return (d[val] * d[w]).sum() / max(d[w].sum(), 1e-9)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", nargs="+", default=["doge"])
    ap.add_argument("--root", default="data/pmfree")
    ap.add_argument("--split", default=None, help="YYYY-MM-DD; default 60%%")
    ap.add_argument("--min-shares", type=float, default=1000)
    ap.add_argument("--top", type=int, default=50)
    ap.add_argument("--lags", nargs="+", type=int, default=[1, 2, 5, 10])
    a = ap.parse_args()

    t = load(a.coins, a.root)
    days = sorted(t.date.unique())
    split = a.split or days[int(len(days) * 0.6)]
    tr, te = t[t.date < split], t[t.date >= split]
    print(f"coins={a.coins} prints={len(t):,} days={len(days)} "
          f"({days[0]}..{days[-1]}) split={split}")
    print(f"train {len(tr):,} prints / {tr.date.nunique()}d   "
          f"test {len(te):,} prints / {te.date.nunique()}d")

    gtr = tr.groupby("wallet").agg(sh=("size", "sum"), n=("size", "size"))
    gtr["c"] = tr.assign(v=tr.net * tr["size"]).groupby("wallet")["v"].sum() \
        / gtr.sh * 100
    gtr = gtr[gtr.sh >= a.min_shares].sort_values("c", ascending=False)
    print(f"\ntrain wallets with >={a.min_shares:,.0f} shares: {len(gtr):,}")

    gte = te.groupby("wallet").agg(sh=("size", "sum"), n=("size", "size"))
    gte["c"] = te.assign(v=te.net * te["size"]).groupby("wallet")["v"].sum() \
        / gte.sh * 100

    j = gtr.join(gte, rsuffix="_te", how="inner")
    j = j[j.sh_te >= 200]
    print(f"of those, {len(j):,} also traded >=200 shares in test")
    if len(j) > 10:
        r = np.corrcoef(j.c, j.c_te)[0, 1]
        rs = pd.Series(j.c).rank().corr(pd.Series(j.c_te).rank())
        print(f"  corr(train c/sh, test c/sh) = {r:+.3f}   "
              f"spearman = {rs:+.3f}")
        for q in (5, 10, 20):
            k = max(len(j) // q, 3)
            topw = j.nlargest(k, "c")
            botw = j.nsmallest(k, "c")
            st = te[te.wallet.isin(topw.index)]
            sb = te[te.wallet.isin(botw.index)]
            print(f"  top 1/{q} by train ({k} wallets): test "
                  f"{wsum(st,'net')*100:+.2f}c/sh on {st['size'].sum():,.0f} sh"
                  f"   |  bottom 1/{q}: {wsum(sb,'net')*100:+.2f}c/sh "
                  f"on {sb['size'].sum():,.0f} sh")

    # ---- step 3: is the top cohort's flow followable? ----
    k = max(len(j) // 10, 3)
    smart = set(j.nlargest(k, "c").index)
    sig = te[te.wallet.isin(smart)].copy()
    print(f"\ncopy test — {len(smart)} smart wallets, {len(sig):,} test prints")
    if not len(sig):
        return
    print(f"  their own test edge: gross {wsum(sig,'gross')*100:+.2f}c  "
          f"net {wsum(sig,'net')*100:+.2f}c")
    te_s = te.sort_values(["slug", "ts"])
    for lag in a.lags:
        rows = []
        for slug, g in te_s.groupby("slug", sort=False):
            s = sig[sig.slug == slug]
            if not len(s):
                continue
            ts = g.ts.values
            px = g.price.values
            du = g.dir_up.values
            uw = g.up_win.values
            for _, r in s.iterrows():
                # first later print betting the SAME direction
                m = (ts >= r.ts + lag) & (du == r.dir_up)
                if not m.any():
                    continue
                i = np.argmax(m)
                p = px[i]
                win = (uw[i] == 1) if du[i] > 0 else (uw[i] == 0)
                rows.append((p, float(win), r["size"]))
        if not rows:
            print(f"  lag {lag}s: no followable prints")
            continue
        d = pd.DataFrame(rows, columns=["p", "win", "size"])
        d["net"] = d.win - d.p - FEE * d.p * (1 - d.p)
        print(f"  lag {lag}s: n={len(d):,}  follow px {d.p.mean():.4f}  "
              f"hit {d.win.mean():.3f}  net {d.net.mean()*100:+.2f}c/share "
              f"(SE {d.net.sem()*100:.2f})")


if __name__ == "__main__":
    main()
