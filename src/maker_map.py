"""The liquidity-provider map: what does the OTHER side of every print earn?

This is the strongest evidence the venue can give, because it needs no fill
assumption. Every print in the public tape IS a maker fill: someone was
resting at that price and got hit. The maker's realised P&L per share is

    maker_ev = p_traded - 1{traded token wins}          (makers pay no fee)

which is exactly minus the taker's gross edge. Averaging that over a
(price, phase) cell gives the realised, fill-conditioned P&L of the actual
liquidity providers in that cell — not a simulation of one.

Caveats this respects:
  * queue position is NOT modelled here; the number is what the marginal
    filled maker earned, so it is an upper bound on a newcomer only to the
    extent that fills are rationed by queue. Sizes are reported so capacity
    is visible.
  * cells are split train/test on the study's frozen 2026-06-15 boundary
    where the data spans it, else on a 60/40 date split.
  * CIs are day-clustered.
"""
import argparse
import glob

import numpy as np
import pandas as pd

FEE = 0.07
PX_BINS = [0, .05, .10, .20, .30, .40, .50, .60, .70, .80, .90, .95, 1.0]
PH_BINS = [-60, 0, 60, 120, 180, 240, 270, 300, 330]


def load(coins, root="data/pmfree"):
    metas, tapes = [], []
    for c in coins:
        mf = sorted(glob.glob(f"data/pmfree/meta/{c}_5m_*.parquet"))
        if not mf:
            continue
        metas.append(pd.concat([pd.read_parquet(f) for f in mf],
                               ignore_index=True))
        for f in sorted(glob.glob(f"{root}/trades/{c}_5m_*.parquet")):
            d = pd.read_parquet(f)
            if len(d):
                d["coin"] = c
                tapes.append(d)
    if not tapes:
        return None
    meta = pd.concat(metas, ignore_index=True).drop_duplicates("slug")
    t = pd.concat(tapes, ignore_index=True).merge(
        meta[["slug", "t0", "up_win"]], on="slug", how="inner")
    t["rel"] = t.ts - t.t0
    t = t[(t.rel >= -60) & (t.rel <= 330)].copy()
    tok_win = np.where(t.is_up_tok, t.up_win, ~t.up_win).astype(float)
    # maker is on the opposite side of the taker's print
    sgn = np.where(t.side == "BUY", 1.0, -1.0)
    t["taker_gross"] = sgn * (tok_win - t.price)
    t["maker_ev"] = -t["taker_gross"]
    t["taker_net"] = t["taker_gross"] - FEE * t.price * (1 - t.price)
    # price of the contract the MAKER ends up holding
    t["maker_px"] = np.where(t.side == "BUY", 1 - t.price, t.price)
    t["date"] = pd.to_datetime(t.t0, unit="s", utc=True).dt.strftime("%Y-%m-%d")
    return t


def clustered(x, w, days):
    d = pd.DataFrame({"x": np.asarray(x, float), "w": np.asarray(w, float),
                      "d": np.asarray(days)})
    d["xw"] = d.x * d.w
    g = d.groupby("d")[["xw", "w"]].sum()
    W = g.w.sum()
    if W <= 0 or len(g) < 3:
        return np.nan, np.nan
    m = g.xw.sum() / W
    infl = (g.xw - g.w * m) / W
    k = len(g)
    return m, float(np.sqrt(max((infl ** 2).sum() * k / (k - 1), 0)))


def cellmap(t, label):
    t = t.copy()
    t["pxb"] = pd.cut(t.maker_px, PX_BINS)
    t["phb"] = pd.cut(t.rel, PH_BINS)
    rows = []
    for (pxb, phb), g in t.groupby(["pxb", "phb"], observed=True):
        if g["size"].sum() < 500:
            continue
        m, se = clustered(g.maker_ev, g["size"], g.date)
        rows.append({"maker_px": str(pxb), "phase_s": str(phb),
                     "shares": g["size"].sum(), "prints": len(g),
                     "maker_c": m * 100,
                     "t": m / se if se and se > 0 else np.nan})
    if not rows:
        return None
    d = pd.DataFrame(rows)
    print(f"\n### {label} — maker cents/share by (price maker holds, "
          f"seconds into window) ###")
    piv = d.pivot(index="maker_px", columns="phase_s", values="maker_c")
    print(piv.to_string(float_format=lambda v: f"{v:+.2f}"))
    sh = d.pivot(index="maker_px", columns="phase_s", values="shares")
    print("\n  shares in each cell:")
    print(sh.to_string(float_format=lambda v: f"{v:,.0f}"))
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", nargs="+", default=["eth", "sol", "xrp", "doge"])
    ap.add_argument("--root", default="data/pmfree")
    ap.add_argument("--split", default=None)
    a = ap.parse_args()
    t = load(a.coins, a.root)
    if t is None:
        print("no tape")
        return
    days = sorted(t.date.unique())
    split = a.split or days[int(len(days) * 0.6)]
    print(f"prints={len(t):,} shares={t['size'].sum():,.0f} "
          f"days={len(days)} ({days[0]}..{days[-1]}) coins={a.coins}")
    print(f"venue-wide: taker gross {clustered(t.taker_gross,t['size'],t.date)[0]*100:+.2f}c  "
          f"taker net {clustered(t.taker_net,t['size'],t.date)[0]*100:+.2f}c  "
          f"=> maker gross {clustered(t.maker_ev,t['size'],t.date)[0]*100:+.2f}c/share")
    print(f"split at {split}")

    dtr = cellmap(t[t.date < split], "TRAIN")
    dte = cellmap(t[t.date >= split], "TEST")
    if dtr is None or dte is None:
        return
    j = dtr.merge(dte, on=["maker_px", "phase_s"], suffixes=("_tr", "_te"))
    j = j[(j.shares_tr >= 2000) & (j.shares_te >= 2000)]
    if len(j) > 5:
        print(f"\ncells present in both slices: {len(j)}   "
              f"corr(train,test) = {np.corrcoef(j.maker_c_tr, j.maker_c_te)[0,1]:+.3f}")
        print("\n--- cells positive in BOTH train and test, ranked by test ---")
        both = j[(j.maker_c_tr > 0) & (j.maker_c_te > 0)].sort_values(
            "maker_c_te", ascending=False)
        print(both[["maker_px", "phase_s", "shares_tr", "maker_c_tr",
                    "shares_te", "maker_c_te", "t_te"]].to_string(
            index=False, float_format=lambda v: f"{v:,.2f}"))


if __name__ == "__main__":
    main()
