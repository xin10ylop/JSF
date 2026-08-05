"""Favorite-longshot bias study across all families, full tape history.

Uses tape-implied quotes at snapshot times (no oracle dependency, so the
whole history counts). Favorite space kills the directional confound; side
splits and phase splits attack it further. Train/test discipline: the split
date is 2026-06-15; everything after is test and only reported at the end.

Usage: python3 src/analysis_flb.py <family> [test]
"""
import sys

import numpy as np
import pandas as pd

FAM_MASTER = {
    "updown_15m_trades": ("data/master_updown.parquet", "15m", 900),
    "updown_5m_trades": ("data/master_updown.parquet", "5m", 300),
    "updown_4h_trades": ("data/master_updown.parquet", "4h", 14400),
    "hourly_updown_trades": ("data/master_hourly.parquet", None, 3600),
    "above_hourly_trades": ("data/master_above.parquet", None, 3600),
}
SPLIT_US = int(pd.Timestamp("2026-06-15").value // 1000)


def snapshots(family, taus=None):
    path, horizon, win = FAM_MASTER[family]
    master = pd.read_parquet(path)
    if horizon:
        master = master[master.horizon == horizon]
    if "t0_us" not in master.columns:  # above family: window = last hour
        master = master.assign(t0_us=master.t1_us - 3600_000_000)
    master = master[master.result >= 0]
    trades = pd.read_parquet(f"data/consolidated/{family}.parquet")
    trades = trades.sort_values("timestamp_us")
    if taus is None:
        taus = [int(win * f) for f in (0.1, 0.3, 0.5, 0.7, 0.85, 0.95)]

    out = []
    tr = dict(iter(trades.groupby("slug")))
    for _, m in master.iterrows():
        tape = tr.get(m.slug)
        if tape is None:
            continue
        tts = tape.timestamp_us.values
        tpx = tape.price.values.astype("float64")
        tbuy = tape.is_buy.values
        for tau in taus:
            t = m.t0_us + tau * 1_000_000
            i = np.searchsorted(tts, t, side="right") - 1
            if i < 0 or (t - tts[i]) > 10_000_000:
                continue
            jb = np.searchsorted(tts, t, side="right") - 1
            # tape-implied ask = last buy print, bid = last sell print (<=t)
            ia = ib = -1
            for j in range(jb, max(jb - 50, -1), -1):
                if tbuy[j] and ia < 0:
                    ia = j
                if not tbuy[j] and ib < 0:
                    ib = j
                if ia >= 0 and ib >= 0:
                    break
            if ia < 0 or ib < 0:
                continue
            if (t - tts[ia]) > 60_000_000 or (t - tts[ib]) > 60_000_000:
                continue
            ask, bid = tpx[ia], tpx[ib]
            if not (0 <= ask - bid <= 0.03):
                continue
            out.append((m.slug, m.t0_us, tau, (ask + bid) / 2, bid, ask,
                        int(m.result)))
    df = pd.DataFrame(out, columns=["slug", "t0_us", "tau", "mid", "bid",
                                    "ask", "result"])
    return df


def flb_report(df, label, B=400):
    df = df.copy()
    df["y"] = (df.result == 0).astype(float)
    df["fav_up"] = df.mid >= 0.5
    df["fav_px"] = np.where(df.fav_up, df.mid, 1 - df.mid)
    df["fav_won"] = np.where(df.fav_up, df.y, 1 - df.y)
    cuts = [0.5, 0.6, 0.7, 0.8, 0.9, 0.97, 1.0]
    base = df.groupby(pd.cut(df.fav_px, cuts), observed=True).agg(
        n=("fav_won", "size"), nm=("slug", "nunique"),
        implied=("fav_px", "mean"), won=("fav_won", "mean"))
    base["gap"] = base.won - base.implied
    slugs = df.slug.unique()
    rng = np.random.default_rng(3)
    dfi = df.set_index("slug")
    gaps = []
    for _ in range(B):
        s = dfi.loc[rng.choice(slugs, len(slugs), replace=True)]
        gb = s.groupby(pd.cut(s.fav_px, cuts), observed=True)
        gaps.append(gb.fav_won.mean() - gb.fav_px.mean())
    G = pd.DataFrame(gaps)
    base["ci_lo"] = G.quantile(0.025).values
    base["ci_hi"] = G.quantile(0.975).values
    up = df[df.fav_up]
    dn = df[~df.fav_up]
    print(f"\n=== {label}: {df.slug.nunique()} mkts, "
          f"{len(df)} snaps ===")
    print(base.round(4).to_string())
    for nm, d in [("UP-favs", up), ("DOWN-favs", dn)]:
        g = d.groupby(pd.cut(d.fav_px, [0.5, 0.7, 0.9, 1.0]), observed=True)
        print(f"{nm}: gap by [.5-.7,.7-.9,.9-1] =",
              (g.fav_won.mean() - g.fav_px.mean()).round(4).tolist())


if __name__ == "__main__":
    fam = sys.argv[1]
    use_test = "test" in sys.argv[2:]
    snap = snapshots(fam)
    tr = snap[snap.t0_us < SPLIT_US]
    te = snap[snap.t0_us >= SPLIT_US]
    flb_report(tr, f"{fam} TRAIN (<2026-06-15)")
    win = FAM_MASTER[fam][2]
    late = tr[tr.tau >= win * 0.85]
    early = tr[tr.tau <= win * 0.5]
    flb_report(late, f"{fam} TRAIN late (tau>=85%)")
    flb_report(early, f"{fam} TRAIN early (tau<=50%)")
    if use_test:
        flb_report(te, f"{fam} TEST (>=2026-06-15)")
