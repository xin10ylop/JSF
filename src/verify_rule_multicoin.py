"""Does the trailing-TWAP contract govern EVERY coin, or only BTC?

The 2026-08-07 rule change was verified on 2,880 settled BTC markets:

    Up  iff  mean(P over [t1-w, t1))  >=  mean(P over [t0-w, t0))

The bot now trades eth, sol, xrp and doge on the assumption that the same
rule applies to them. That assumption was never tested, and it drives two
things that must both be right: the z-score the strategy fires on, and the
outcome the paper broker settles against. If doge still settles on the end
price, every doge fill is scored against the wrong contract.

This compares both candidate rules against the venue's own resolved
outcomes, per coin, on each side of the change. Binance 1s klines stand in
for the Chainlink series; a constant basis cancels in both rules, so the
comparison is fair even though the level is not exact.

    python3 src/verify_rule_multicoin.py
"""
import argparse
import glob
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "src")
from rollavg_edge_test import load_1s, CHANGE  # noqa: E402


def check(coin, fam, era):
    w = 30 if fam == "5m" else 60
    mf = sorted(glob.glob(f"data/pmfree/meta/{coin}_{fam}_*.parquet"))
    if not mf:
        return None
    m = pd.concat([pd.read_parquet(f) for f in mf],
                  ignore_index=True).drop_duplicates("slug")
    m = m[(m.t0 >= CHANGE) if era == "post" else (m.t0 < CHANGE)]
    m = m.dropna(subset=["t0", "t1", "up_win"])
    if not len(m):
        return None
    days = sorted(pd.to_datetime(m.t0, unit="s", utc=True)
                  .dt.strftime("%Y-%m-%d").unique())
    px = load_1s(coin, days)
    if px is None or not len(px):
        return None
    px = px.copy()
    px.index = px.index + 1              # klines are stamped by OPEN time
    lo = px.index[0]
    v = px.values.astype("float64")
    cs = np.concatenate([[0.0], np.cumsum(v)])

    def mean_over(a, b):
        ia = np.clip(a - lo, 0, len(v)); ib = np.clip(b - lo, 0, len(v))
        n = ib - ia
        return np.where(n > 0, (cs[ib] - cs[ia]) / np.maximum(n, 1), np.nan)

    def at(x):
        i = np.clip(x - lo, 0, len(v) - 1)
        return np.where((x - lo >= 0) & (x - lo < len(v)), v[i], np.nan)

    t0 = m.t0.values.astype("int64")
    t1 = m.t1.values.astype("int64")
    twap = mean_over(t1 - w, t1) >= mean_over(t0 - w, t0)   # the new rule
    endp = at(t1) >= at(t0)                                 # the old rule
    ok = np.isfinite(mean_over(t1 - w, t1)) & np.isfinite(at(t1))
    won = m.up_win.values.astype(bool)
    if ok.sum() < 30:
        return None
    disagree = ok & (twap != endp)
    return {
        "n": int(ok.sum()),
        "twap": float((twap[ok] == won[ok]).mean()),
        "end": float((endp[ok] == won[ok]).mean()),
        "n_dis": int(disagree.sum()),
        "twap_wins": (float((twap[disagree] == won[disagree]).mean())
                      if disagree.sum() else float("nan")),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", default="btc,eth,sol,xrp,doge")
    ap.add_argument("--fams", default="5m,15m")
    a = ap.parse_args()
    print("Which rule matches the venue's settled outcomes?")
    print("  twap = mean(P,[t1-w,t1)) >= mean(P,[t0-w,t0))   (post-change)")
    print("  end  = P(t1) >= P(t0)                           (pre-change)")
    print("  the decisive column is the LAST one: on markets where the two "
          "rules disagree,\n  how often is trailing-TWAP the one that is "
          "right? >0.5 means TWAP governs.\n")
    print(f"{'coin':<6}{'fam':<5}{'era':<6}{'n':>7}{'twap':>8}{'end':>8}"
          f"{'disagree':>10}{'twap right':>12}")
    for coin in a.coins.split(","):
        for fam in a.fams.split(","):
            for era in ("pre", "post"):
                r = check(coin, fam, era)
                if r is None:
                    continue
                tw = ("  n/a" if not np.isfinite(r["twap_wins"])
                      else f"{r['twap_wins']:.3f}")
                print(f"{coin:<6}{fam:<5}{era:<6}{r['n']:>7,}"
                      f"{r['twap']:>8.4f}{r['end']:>8.4f}"
                      f"{r['n_dis']:>10,}{tw:>12}")


if __name__ == "__main__":
    main()
