"""Print-level venue economics: every trade is a maker fill.

For each print in a resolved market: the maker side bought at the bid (sell
print) or sold at the ask (buy print). E[outcome - price | print side]
by price bucket and window phase = fill-conditional maker/taker alpha,
gross of fees (makers pay none; takers pay 0.07*p*(1-p)).

Usage: python3 src/analysis_prints.py <family> [test]
"""
import sys

import numpy as np
import pandas as pd

FAM = {
    "updown_15m_trades": ("data/master_updown.parquet", "15m", 900),
    "updown_5m_trades": ("data/master_updown.parquet", "5m", 300),
    "updown_4h_trades": ("data/master_updown.parquet", "4h", 14400),
    "hourly_updown_trades": ("data/master_hourly.parquet", None, 3600),
    "above_hourly_trades": ("data/master_above.parquet", None, 3600),
}
SPLIT_US = int(pd.Timestamp("2026-06-15").value // 1000)
FEE = 0.07


def load(family):
    path, horizon, win = FAM[family]
    m = pd.read_parquet(path)
    if horizon:
        m = m[m.horizon == horizon]
    if "t0_us" not in m.columns:
        m = m.assign(t0_us=m.t1_us - 3600_000_000)
    m = m[m.result >= 0][["slug", "t0_us", "result"]]
    t = pd.read_parquet(f"data/consolidated/{family}.parquet",
                        columns=["timestamp_us", "slug", "price", "size",
                                 "is_buy"])
    t = t.merge(m, on="slug", how="inner")
    t["tau"] = (t.timestamp_us - t.t0_us) / 1e6
    t = t[(t.tau >= 0) & (t.tau <= win)]
    t["y"] = (t.result == 0).astype("float32")
    t["phase"] = pd.cut(t.tau / win, [0, 0.33, 0.66, 0.9, 0.985, 1.0],
                        labels=["early", "mid", "late", "final", "endgame"])
    return t, win


def report(t, label, weight_by_size=True):
    t = t.copy()
    # maker POV: sell print -> maker bought Up at price; buy print -> maker
    # sold Up at price (equivalently bought Down at 1-price).
    t["mk_entry"] = np.where(t.is_buy, 1 - t.price, t.price)
    t["mk_win"] = np.where(t.is_buy, 1 - t.y, t.y)
    t["mk_alpha"] = t.mk_win - t.mk_entry
    t["tk_entry"] = np.where(t.is_buy, t.price, 1 - t.price)
    t["tk_win"] = np.where(t.is_buy, t.y, 1 - t.y)
    t["tk_fee"] = FEE * t.price * (1 - t.price)
    t["tk_alpha"] = t.tk_win - t.tk_entry - t.tk_fee
    t["w"] = t["size"].clip(0, 2000) if weight_by_size else 1.0
    t["bucket"] = pd.cut(t.mk_entry, [0, .1, .3, .5, .7, .9, .97, 1.])

    def agg(d):
        w = d.w.values
        return pd.Series({
            "n": len(d),
            "mkts": d.slug.nunique(),
            "$vol": (d.price * d["size"]).sum(),
            "mk_entry": np.average(d.mk_entry, weights=w),
            "mk_alpha_c": 100 * np.average(d.mk_alpha, weights=w),
            "tk_alpha_c": 100 * np.average(d.tk_alpha, weights=w),
        })

    print(f"\n=== {label}: {t.slug.nunique()} mkts, {len(t)} prints ===")
    by_phase = t.groupby("phase", observed=True).apply(agg, include_groups=False)
    print("BY PHASE (maker buys at print price; alpha in cents/share, "
          "size-weighted):")
    print(by_phase.round(2).to_string())
    by_bucket = t.groupby(["phase", "bucket"], observed=True).apply(
        agg, include_groups=False)
    print("\nBY PHASE x MAKER-ENTRY BUCKET:")
    print(by_bucket.round(2).to_string())


def cluster_ci(t, sel_label, mask, B=300):
    d = t[mask]
    pm = d.groupby("slug").apply(
        lambda x: np.average(x.mk_alpha, weights=x.w), include_groups=False)
    rng = np.random.default_rng(7)
    bs = [np.mean(rng.choice(pm.values, len(pm))) for _ in range(B)]
    lo, hi = np.quantile(bs, [0.025, 0.975])
    print(f"{sel_label}: mk_alpha {100*pm.mean():.2f}c "
          f"CI[{100*lo:.2f},{100*hi:.2f}] ({len(pm)} mkts)")


if __name__ == "__main__":
    fam = sys.argv[1]
    use_test = "test" in sys.argv[2:]
    t, win = load(fam)
    part = t[t.t0_us >= SPLIT_US] if use_test else t[t.t0_us < SPLIT_US]
    tag = f"{fam} {'TEST' if use_test else 'TRAIN'}"
    t["w"] = t["size"].clip(0, 2000)
    report(part, tag)
    part = part.copy()
    part["mk_entry"] = np.where(part.is_buy, 1 - part.price, part.price)
    part["mk_win"] = np.where(part.is_buy, 1 - part.y, part.y)
    part["mk_alpha"] = part.mk_win - part.mk_entry
    part["w"] = part["size"].clip(0, 2000)
    for lo, hi in [(0.5, 0.7), (0.7, 0.9), (0.9, 0.97), (0.97, 1.0)]:
        cluster_ci(part, f"maker-buy fav {lo}-{hi} (all phases)",
                   (part.mk_entry >= lo) & (part.mk_entry < hi))
