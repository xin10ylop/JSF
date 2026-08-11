"""Does the ask we aimed at still exist when our order arrives?

The paper broker's central fiction is that we win the race for the
displayed size. This measures the race directly from recorded books: for
every snapshot, look forward by the real round-trip delay and ask

  1. is there still an ask at or below the price we saw?          (reachable)
  2. how many shares are resting at or below it?                  (fillable)

Polymarket holds taker orders on crypto up/down markets for 250 ms before
matching (docs: "the order is held for 250 ms, then validation runs again
and the order is matched or placed on the book"), so the honest delay is
250 ms plus the network round trip -- not the 150 ms the config guessed.

    python3 src/ask_survival.py --delay-ms 400
"""
import argparse
import glob

import numpy as np
import pandas as pd


def best(px_list, sz_list):
    """Best (lowest) ask and its size from the worst-to-best level arrays."""
    if px_list is None or len(px_list) == 0:
        return np.nan, 0.0
    p = np.asarray(px_list, dtype="float64")
    s = np.asarray(sz_list, dtype="float64")
    i = int(np.argmin(p))
    return float(p[i]), float(s[i])


def size_at_or_below(px_list, sz_list, limit):
    if px_list is None or len(px_list) == 0:
        return 0.0
    p = np.asarray(px_list, dtype="float64")
    s = np.asarray(sz_list, dtype="float64")
    return float(s[p <= limit + 1e-9].sum())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delay-ms", type=float, default=400.0)
    ap.add_argument("--max-stale-ms", type=float, default=1500.0,
                    help="skip if the next snapshot after the delay is older "
                         "than this (we cannot see what happened)")
    ap.add_argument("--min-rem", type=float, default=2.0)
    ap.add_argument("--max-rem", type=float, default=60.0)
    a = ap.parse_args()

    fs = sorted(glob.glob("data/live/books/*.parquet"))
    d = pd.concat([pd.read_parquet(f) for f in fs], ignore_index=True)
    d = d[(d.rem5 >= a.min_rem) & (d.rem5 <= a.max_rem)].copy()
    d = d.sort_values(["asset_id", "t_local_us"]).reset_index(drop=True)

    rows = []
    delay_us = a.delay_ms * 1000.0
    for aid, g in d.groupby("asset_id", sort=False):
        t = g.t_local_us.values.astype("int64")
        apx = g.ask_px.values
        asz = g.ask_sz.values
        # index of the first snapshot at or after t_i + delay
        j = np.searchsorted(t, t + int(delay_us), side="left")
        for i in range(len(t)):
            k = j[i]
            if k >= len(t):
                continue
            # the state we act on must be the state AFTER the delay; if the
            # book went quiet for a long time the snapshot is uninformative
            if (t[k] - (t[i] + delay_us)) > a.max_stale_ms * 1000.0:
                continue
            p0, s0 = best(apx[i], asz[i])
            if not np.isfinite(p0) or s0 <= 0:
                continue
            p1, _ = best(apx[k], asz[k])
            avail = size_at_or_below(apx[k], asz[k], p0)
            rows.append((p0, s0, p1, avail, g.rem5.values[i]))

    r = pd.DataFrame(rows, columns=["p0", "s0", "p1", "avail", "rem"])
    if not len(r):
        print("no comparable snapshot pairs")
        return
    r["reachable"] = r.avail > 0
    r["filled_frac"] = np.minimum(r.avail / r.s0, 1.0)
    print(f"=== ask survival over {a.delay_ms:.0f} ms "
          f"({len(r):,} snapshot pairs, rem {a.min_rem:g}-{a.max_rem:g}s) ===")
    print(f"price still reachable   {r.reachable.mean():.1%} of the time")
    print(f"of the size we saw, still there: mean {r.filled_frac.mean():.1%}  "
          f"median {r.filled_frac.median():.1%}")
    print(f"price improved (p1<p0)  {(r.p1 < r.p0 - 1e-9).mean():.1%}   "
          f"same {(abs(r.p1 - r.p0) <= 1e-9).mean():.1%}   "
          f"worse {(r.p1 > r.p0 + 1e-9).mean():.1%}")
    print("\nby the price we aimed at:")
    b = pd.cut(r.p0, [0, .3, .5, .7, .85, .92, .95, .98, 1.0])
    print(r.groupby(b, observed=True).apply(lambda x: pd.Series({
        "n": len(x),
        "reachable": x.reachable.mean(),
        "size_kept": x.filled_frac.mean(),
        "seen_sz": x.s0.median(),
    }), include_groups=False).to_string(float_format=lambda v: f"{v:,.3f}"))
    print("\nby seconds remaining:")
    b2 = pd.cut(r.rem, [0, 5, 10, 20, 30, 45, 60])
    print(r.groupby(b2, observed=True).apply(lambda x: pd.Series({
        "n": len(x),
        "reachable": x.reachable.mean(),
        "size_kept": x.filled_frac.mean(),
    }), include_groups=False).to_string(float_format=lambda v: f"{v:,.3f}"))


if __name__ == "__main__":
    main()
