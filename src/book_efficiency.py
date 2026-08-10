"""H39 stage 2 — does each coin's BOOK price the reversal?

Uses the free trade tape. Every print is normalised to Up-token terms and
classified by which side of the book it consumed, so the prices used here
are prices at which a trade DEMONSTRABLY happened:

    ask_up  (a taker could have bought Up here)
        up-token BUY at p        -> p
        down-token SELL at q     -> 1-q
    bid_up  (a taker could have sold Up / bought Down here)
        up-token SELL at p       -> p
        down-token BUY at q      -> 1-q

For each market we take the prints inside [t0+lo, t0+hi] (default the first
30s of the window) and reduce to a size-weighted ask_up / bid_up.

Then, per AHL score bucket:
    market-implied P(Up)  = mid of (bid_up, ask_up)
    actual P(Up)          = settled outcome frequency
    residual              = actual - implied     <-- the only tradeable part

and the executable taker EV, net of the verified fee 0.07*p*(1-p):
    score<0 -> buy Up  at ask_up : EV = P(Up)      - ask_up - fee(ask_up)
    score>0 -> buy Down at ask_dn: EV = (1-P(Up))  - ask_dn - fee(ask_dn),
                                   ask_dn = 1 - bid_up
"""
import argparse
import glob
import os

import numpy as np
import pandas as pd

SPLIT = pd.Timestamp("2026-06-15", tz="UTC")
FEE = 0.07


def fee(p):
    return FEE * p * (1 - p)


def load_trades(coin, root, t_lo, t_hi):
    """Reduce the tape to one row per market: size-weighted bid/ask in a
    window measured in seconds relative to t0."""
    fs = sorted(glob.glob(f"{root}/trades/{coin}_5m_*.parquet"))
    if not fs:
        return None
    meta = pd.concat([pd.read_parquet(f) for f in
                      sorted(glob.glob(f"data/pmfree/meta/{coin}_5m_*.parquet"))],
                     ignore_index=True)[["slug", "t0"]].drop_duplicates("slug")
    out = []
    for f in fs:
        d = pd.read_parquet(f)
        if not len(d):
            continue
        d = d.merge(meta, on="slug", how="inner")
        d["rel"] = d["ts"] - d["t0"]
        d = d[(d.rel >= t_lo) & (d.rel < t_hi)]
        if not len(d):
            continue
        is_ask = ((d.is_up_tok) & (d.side == "BUY")) | \
                 ((~d.is_up_tok) & (d.side == "SELL"))
        d = d.assign(is_ask=is_ask, w=d["size"], wp=d["size"] * d["p_up"])
        g = d.groupby(["slug", "is_ask"]).agg(w=("w", "sum"),
                                              wp=("wp", "sum"),
                                              n=("w", "size")).reset_index()
        g["vw"] = g.wp / g.w
        piv = g.pivot(index="slug", columns="is_ask",
                      values=["vw", "w", "n"])
        res = pd.DataFrame(index=piv.index)
        for src, dst in (("vw", "up"), ("w", "sz"), ("n", "n")):
            for flag, side in ((True, "ask"), (False, "bid")):
                col = (src, flag)
                res[f"{side}_{dst}" if src != "vw" else f"{side}_up"] = (
                    piv[col] if col in piv.columns else np.nan)
        out.append(res)
    if not out:
        return None
    return pd.concat(out).reset_index()


def clustered(x, days):
    """Mean and day-clustered SE of a per-trade series."""
    s = pd.DataFrame({"x": np.asarray(x, float), "d": np.asarray(days)})
    g = s.groupby("d")["x"].agg(["sum", "size"])
    n = g["size"].sum()
    m = g["sum"].sum() / n
    k = len(g)
    if k < 3 or n == 0:
        return m, np.nan
    # influence of each cluster on the pooled mean
    infl = (g["sum"] - g["size"] * m) / n
    var = (infl ** 2).sum() * k / (k - 1)
    return m, float(np.sqrt(max(var, 0)))


def bucket_report(df, coin, label, tag):
    print(f"\n--- {coin.upper()} [{label}] {tag} ---")
    print(f"{'score':>6} {'n':>7} {'bid_up':>7} {'ask_up':>7} {'mid':>7} "
          f"{'actual':>7} {'resid_pp':>9} {'takerEV_c':>10} {'t':>6} "
          f"{'makerEV_c':>10}")
    evs, evms, dys = [], [], []
    for sc in (-4, 4):
        h = df[(df.score == sc) & df.ask_up.notna() & df.bid_up.notna()]
        if len(h) < 30:
            continue
        bid, ask = h.bid_up.mean(), h.ask_up.mean()
        mid = (bid + ask) / 2
        act = h.up_win.mean()
        if sc < 0:                      # buy Up at the ask
            ev = h.up_win - h.ask_up - fee(h.ask_up)
            evm = h.up_win - h.bid_up               # join the Up bid
        else:                           # buy Down at its ask = 1 - bid_up
            ask_dn = 1 - h.bid_up
            ev = (1 - h.up_win) - ask_dn - fee(ask_dn)
            evm = (1 - h.up_win) - (1 - h.ask_up)   # join the Down bid
        m, se = clustered(ev, h.day)
        t = m / se if se and np.isfinite(se) and se > 0 else np.nan
        print(f"{sc:>6} {len(h):>7,} {bid:>7.4f} {ask:>7.4f} {mid:>7.4f} "
              f"{act:>7.4f} {(act-mid)*100:>+9.2f} {m*100:>+10.2f} "
              f"{t:>+6.2f} {evm.mean()*100:>+10.2f}")
        evs.append(ev); evms.append(evm); dys.append(h.day)
    if len(evs) == 2:
        ev = pd.concat(evs); evm = pd.concat(evms); dy = pd.concat(dys)
        m, se = clustered(ev, dy)
        mm, _ = clustered(evm, dy)
        t = m / se if se and np.isfinite(se) and se > 0 else np.nan
        lo, hi = m - 1.96 * se, m + 1.96 * se
        print(f"   POOLED n={len(ev):,}  takerEV={m*100:+.2f}c "
              f"[{lo*100:+.2f},{hi*100:+.2f}] t={t:+.2f}  |  "
              f"makerEV(assumed fill)={mm*100:+.2f}c")
        return m, se, len(ev)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", nargs="+", default=["eth", "sol", "xrp", "doge", "btc"])
    ap.add_argument("--root", default="data/pmfree_x4")
    ap.add_argument("--lo", type=int, default=0)
    ap.add_argument("--hi", type=int, default=30)
    ap.add_argument("--min-sz", type=float, default=0.0)
    a = ap.parse_args()

    rev = pd.read_parquet("data/rev_multicoin.parquet")
    rev["d"] = pd.to_datetime(rev.t0, unit="s", utc=True)

    print("=" * 100)
    print(f"H39 STAGE 2 — book efficiency vs the reversal, prints in "
          f"[t0{a.lo:+d}s, t0{a.hi:+d}s)")
    print("=" * 100)
    for coin in a.coins:
        t = load_trades(coin, a.root, a.lo, a.hi)
        if t is None:
            print(f"\n{coin}: no tape yet")
            continue
        df = rev[rev.coin == coin].merge(t, on="slug", how="inner")
        if a.min_sz:
            df = df[(df.ask_sz.fillna(0) >= a.min_sz) &
                    (df.bid_sz.fillna(0) >= a.min_sz)]
        cov = len(df) / max(len(rev[(rev.coin == coin) & (rev.score.abs() >= 4)]), 1)
        print(f"\n===== {coin.upper()}  matched {len(df):,} markets "
              f"(tape coverage of extreme-score set {cov:.0%}) =====")
        for label, sub in (("train", df[df.d < SPLIT]),
                           ("test", df[df.d >= SPLIT]),
                           ("all", df)):
            if len(sub) < 100:
                continue
            bucket_report(sub, coin, label,
                          f"{sub.d.min().date()}..{sub.d.max().date()}")


if __name__ == "__main__":
    main()
