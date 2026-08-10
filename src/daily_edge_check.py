"""Daily decay monitor for the post-2026-08-07 endgame edge.

The edge is a re-pricing lag, so the question is not "is it real" (three
days of data say yes) but "is it still there today". This fetches one day
of free data for every coin, re-runs the endgame measurement with frozen
parameters, and appends a row to reports/decay_log.csv.

Frozen parameters -- do NOT tune these, that is the point of the monitor:
    gate      |z| >= 2.0
    region    inside the settle window only (rem <= w)
    fee       0.07 * p * (1-p)
    prices    real prints only

Run: python3 src/daily_edge_check.py --day 2026-08-10
     python3 src/daily_edge_check.py            (defaults to yesterday UTC)
"""
import argparse
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ZMIN = 2.0
FEE = 0.07
LOG = "reports/decay_log.csv"
COINS = ["btc", "eth", "sol", "xrp", "doge"]


def sh(cmd):
    print("  $", " ".join(cmd), flush=True)
    return subprocess.run(cmd, capture_output=True, text=True).returncode


def ensure_data(day, coins):
    sh([sys.executable, "src/fetch_pm_free.py", "meta", "--coins", *coins,
        "--start", day, "--end", day, "--workers", "8", "--out",
        "data/pmfree"])
    sh([sys.executable, "src/fetch_klines_rest.py", "--coins", *coins,
        "--days", day])
    sh([sys.executable, "src/fetch_pm_free.py", "trades", "--coins", *coins,
        "--start", day, "--end", day, "--workers", "16", "--out",
        "data/pmfree_aug"])


def measure(day, coins):
    from endgame_rollavg import build
    rows = []
    for c in coins:
        try:
            d = build(c, "data/pmfree_aug", 240, days_filter=[day], era="post")
        except Exception as e:  # noqa: BLE001
            print(f"  {c}: build failed ({e})")
            continue
        if d is None or not len(d):
            continue
        d = d[d.inside & (d.date == day)]
        if not len(d):
            continue
        up = d[(d.z >= ZMIN) & d.is_ask]
        dn = d[(d.z <= -ZMIN) & (~d.is_ask)]
        px = np.concatenate([up.p_up.values, 1 - dn.p_up.values])
        win = np.concatenate([up.up_win.values.astype(float),
                              (~dn.up_win.values).astype(float)])
        sz = np.concatenate([up["size"].values, dn["size"].values])
        if sz.sum() <= 0:
            continue
        ev = win - px - FEE * px * (1 - px)
        rows.append({"day": day, "coin": c, "prints": len(px),
                     "shares": float(sz.sum()),
                     "avg_px": float((px * sz).sum() / sz.sum()),
                     "hit": float((win * sz).sum() / sz.sum()),
                     "ev_c": float((ev * sz).sum() / sz.sum() * 100),
                     "pool_usd": float((ev * sz).sum())})
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=None)
    ap.add_argument("--coins", nargs="+", default=COINS)
    ap.add_argument("--skip-fetch", action="store_true")
    a = ap.parse_args()
    day = a.day or (datetime.now(timezone.utc) -
                    timedelta(days=1)).strftime("%Y-%m-%d")
    print(f"daily edge check for {day} (frozen gate |z|>={ZMIN})")
    if not a.skip_fetch:
        ensure_data(day, a.coins)
    df = measure(day, a.coins)
    if not len(df):
        print("no measurable prints for that day")
        return
    tot_sh = df.shares.sum()
    pooled = (df.ev_c * df.shares).sum() / tot_sh
    print("\n" + df.to_string(index=False,
                              float_format=lambda v: f"{v:,.2f}"))
    print(f"\nPOOLED {day}: {pooled:+.2f}c/share on {tot_sh:,.0f} shares "
          f"({df.pool_usd.sum():,.0f} USD of edge in the pool)")
    os.makedirs("reports", exist_ok=True)
    df.to_csv(LOG, mode="a", header=not os.path.exists(LOG), index=False)
    hist = pd.read_csv(LOG).drop_duplicates(["day", "coin"], keep="last")
    hist.to_csv(LOG, index=False)
    piv = hist.pivot_table(index="coin", columns="day", values="ev_c")
    print("\ndecay history (c/share):")
    print(piv.to_string(float_format=lambda v: f"{v:+.2f}"))
    daily = hist.groupby("day").apply(
        lambda x: (x.ev_c * x.shares).sum() / x.shares.sum())
    print("\npooled by day:")
    print(daily.to_string(float_format=lambda v: f"{v:+.2f}"))


if __name__ == "__main__":
    main()
