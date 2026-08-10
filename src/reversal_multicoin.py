"""H39: does the ORDER BOOK of each coin price the short-horizon reversal?

Stage 1 (this file, --stage under): measure the reversal in the OUTCOME for
every coin on the full free-metadata sample (229K resolved 5m markets,
2026-03-01..2026-08-09).

Stage 2 (--stage book): join the free trade tape and compare what the market
actually charged at window open against what happened, per score bucket.
The residual (actual - market) is the only thing that can be traded; it must
clear the taker fee 0.07*p*(1-p) ~ 1.75c at p=0.5 to be takeable, or clear 0
to be makeable.

AHL/ManHL multi-horizon momentum score, computed at window open t0:
    score = sum_k sign(C[t0] - C[t0 - k bars]),  k in {5,10,21,42}, 5m bars
The BTC result was that the signal is real but INVERTED at this horizon
(short-term reversal) and that BTC's book already fades it.

Split: train < 2026-06-15 <= test (the study's frozen split).
"""
import argparse
import glob
import os

import numpy as np
import pandas as pd

LAGS = (5, 10, 21, 42)
SPLIT = pd.Timestamp("2026-06-15", tz="UTC")
COINS = ["btc", "eth", "sol", "xrp", "doge"]


def load_meta(coins, horizon="5m"):
    fs = []
    for c in coins:
        fs += sorted(glob.glob(f"data/pmfree/meta/{c}_{horizon}_*.parquet"))
    df = pd.concat([pd.read_parquet(f) for f in fs], ignore_index=True)
    return df.drop_duplicates("slug")


def add_score(meta):
    """Attach the AHL score at t0 plus realised vol, per coin."""
    out = []
    for coin, g in meta.groupby("coin"):
        b = pd.read_parquet(f"data/altbars/{coin}_5m.parquet")
        s = pd.Series(b["close"].values, index=b["t"].values).astype(float)
        idx = g["t0"].values
        c0 = s.reindex(idx).values
        score = np.zeros(len(idx))
        ok = ~np.isnan(c0)
        for k in LAGS:
            ck = s.reindex(idx - k * 300).values
            with np.errstate(invalid="ignore"):
                score += np.sign(c0 - ck)
            ok &= ~np.isnan(ck)
        # trailing realised vol (12 bars = 1h) for conditioning
        r = np.log(s / s.shift(1))
        rv = r.rolling(12).std()
        g = g.copy()
        g["score"] = np.where(ok, score, np.nan)
        g["c0"] = c0
        g["rv1h"] = rv.reindex(idx).values
        # forward 5m return of the underlying (sanity / futures view)
        c1 = s.reindex(idx + 300).values
        g["fwd_bps"] = (c1 / c0 - 1.0) * 1e4
        out.append(g)
    return pd.concat(out, ignore_index=True)


def wilson(p, n, z=1.96):
    if n == 0:
        return (np.nan, np.nan)
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (c - h, c + h)


def daycluster_se(df, col):
    """Cluster-robust SE by UTC day."""
    d = df.groupby(df["day"])[col].agg(["mean", "size"])
    if len(d) < 3:
        return np.nan
    w = d["size"] / d["size"].sum()
    m = (w * d["mean"]).sum()
    v = (w ** 2 * d["mean"].sub(m).pow(2) * len(d) / max(len(d) - 1, 1)).sum()
    return float(np.sqrt(max(v, 0)))


def report_under(df):
    print("\n" + "=" * 96)
    print("STAGE 1 — the OUTCOME reversal, full free sample (5m up/down)")
    print("=" * 96)
    for coin in COINS:
        g = df[(df.coin == coin) & df.score.notna()]
        if not len(g):
            continue
        print(f"\n--- {coin.upper()}  n={len(g):,}  "
              f"{g.d.min().date()}..{g.d.max().date()} ---")
        print(f"{'score':>6} {'n_train':>8} {'P(Up)tr':>8} "
              f"{'n_test':>8} {'P(Up)te':>8} {'n_all':>8} {'P(Up)all':>9} "
              f"{'95% CI all':>18}")
        for sc in (-4, -2, 0, 2, 4):
            h = g[g.score == sc]
            tr = h[h.d < SPLIT]
            te = h[h.d >= SPLIT]
            p = h.up_win.mean() if len(h) else np.nan
            lo, hi = wilson(p, len(h))
            print(f"{sc:>6} {len(tr):>8,} {tr.up_win.mean():>8.4f} "
                  f"{len(te):>8,} {te.up_win.mean():>8.4f} "
                  f"{len(h):>8,} {p:>9.4f}   [{lo:.4f},{hi:.4f}]")
        # pooled directional spread: P(Up|score<0) - P(Up|score>0)
        for lbl, sub in (("train", g[g.d < SPLIT]), ("test", g[g.d >= SPLIT]),
                         ("all", g)):
            neg = sub[sub.score < 0]
            pos = sub[sub.score > 0]
            if not len(neg) or not len(pos):
                continue
            sp = neg.up_win.mean() - pos.up_win.mean()
            se = np.sqrt(neg.up_win.var() / len(neg) + pos.up_win.var() / len(pos))
            print(f"   spread P(Up|score<0)-P(Up|score>0) [{lbl:5s}] = "
                  f"{sp*100:+.2f}pp  t={sp/se:+.2f}  "
                  f"(n={len(neg):,}/{len(pos):,})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="under", choices=["under", "book"])
    ap.add_argument("--coins", nargs="+", default=COINS)
    ap.add_argument("--cache", default="data/rev_multicoin.parquet")
    a = ap.parse_args()

    if os.path.exists(a.cache):
        df = pd.read_parquet(a.cache)
    else:
        meta = load_meta(a.coins)
        df = add_score(meta)
        df["d"] = pd.to_datetime(df.t0, unit="s", utc=True)
        df["day"] = df.d.dt.strftime("%Y-%m-%d")
        df.to_parquet(a.cache, index=False)
    df["d"] = pd.to_datetime(df.t0, unit="s", utc=True)
    if a.stage == "under":
        report_under(df)


if __name__ == "__main__":
    main()
