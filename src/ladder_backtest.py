"""Resurrection-ladder backtest.

At tau = POST_FRAC of the window, post maker bids at fixed deep levels on
BOTH sides (Up and Down). Orders rest until window end. Fills via the
conservative print rule (prints strictly below level always fill; prints at
level fill beyond queue). Positions redeem at settlement.

The bet: conditional on a side collapsing through deep levels late, the
crowd oversells - the fill price is below the true resurrection probability
(fat tails + panic). Fee-free (maker).

Usage: POST_FRAC=0.9 QUEUE=200 python3 src/ladder_backtest.py 15m [test]
Env: LEVELS="0.05,0.10,0.15,0.20,0.25"  SIZE=100
"""
import os
import sys

import numpy as np
import pandas as pd

SPLIT_US = int(pd.Timestamp("2026-06-15").value // 1000)
WIN_S = {"5m": 300, "15m": 900, "4h": 14400}


def run(horizon, use_test=False):
    fam = f"updown_{horizon}_trades"
    win = WIN_S[horizon]
    post_frac = float(os.environ.get("POST_FRAC", 0.9))
    queue = float(os.environ.get("QUEUE", 200.0))
    size = float(os.environ.get("SIZE", 100.0))
    levels = [float(x) for x in
              os.environ.get("LEVELS", "0.05,0.10,0.15,0.20,0.25").split(",")]

    m = pd.read_parquet("data/master_updown.parquet",
                        columns=["slug", "horizon", "t0_us", "result"])
    m = m[(m.horizon == horizon) & (m.result >= 0)]
    m = m[m.t0_us >= SPLIT_US] if use_test else m[m.t0_us < SPLIT_US]
    res = dict(zip(m.slug, m.result))
    t0s = dict(zip(m.slug, m.t0_us))

    t = pd.read_parquet(f"data/consolidated/{fam}.parquet",
                        columns=["timestamp_us", "slug", "price", "size",
                                 "is_buy"])
    t = t[t.slug.isin(res)]
    t = t.sort_values(["slug", "timestamp_us"])

    post_off = int(post_frac * win * 1e6)
    fills = []
    for slug, tape in t.groupby("slug"):
        result = res[slug]
        t0 = t0s[slug]
        t_post = t0 + post_off
        t_end = t0 + win * 1_000_000
        tts = tape.timestamp_us.values
        i0 = np.searchsorted(tts, t_post, side="right")
        i1 = np.searchsorted(tts, t_end, side="right")
        if i1 <= i0:
            continue
        tpx = tape.price.values[i0:i1].astype("float64")
        tsz = tape["size"].values[i0:i1].astype("float64")
        tbuy = tape.is_buy.values[i0:i1]
        # Up-side ladder: sell prints (is_buy False) at price <= level
        # Down-side ladder: buy prints at price >= 1 - level
        for lvl in levels:
            for side in ("Up", "Down"):
                q = queue
                rem = size
                got = 0.0
                if side == "Up":
                    mask_thr = ~tbuy
                    for p, s, ok in zip(tpx, tsz, mask_thr):
                        if not ok or rem <= 0:
                            continue
                        if p < lvl - 1e-9:
                            take = min(rem, s)
                        elif abs(p - lvl) <= 1e-9:
                            beyond = max(0.0, s - q)
                            q = max(0.0, q - s)
                            take = min(rem, beyond)
                        else:
                            continue
                        rem -= take
                        got += take
                else:
                    lvl_up = 1 - lvl
                    for p, s, ok in zip(tpx, tsz, tbuy):
                        if not ok or rem <= 0:
                            continue
                        if p > lvl_up + 1e-9:
                            take = min(rem, s)
                        elif abs(p - lvl_up) <= 1e-9:
                            beyond = max(0.0, s - q)
                            q = max(0.0, q - s)
                            take = min(rem, beyond)
                        else:
                            continue
                        rem -= take
                        got += take
                if got > 0:
                    won = (side == "Up" and result == 0) or \
                          (side == "Down" and result == 1)
                    pnl = got * ((1.0 if won else 0.0) - lvl)
                    fills.append((slug, t0, side, lvl, got, won, pnl))

    if not fills:
        print("NO FILLS")
        return
    F = pd.DataFrame(fills, columns=["slug", "t0_us", "side", "level",
                                     "shares", "won", "pnl"])
    F["day"] = pd.to_datetime(F.t0_us, unit="us").dt.date
    stake = (F.shares * F.level).sum()
    print(f"POST_FRAC={post_frac} QUEUE={queue} SIZE={size} LEVELS={levels} "
          f"{'TEST' if use_test else 'TRAIN'}")
    print(f"fills: {len(F)} on {F.slug.nunique()} mkts, {F.day.nunique()} days; "
          f"{F.shares.sum():.0f} sh, ${stake:.0f} stake")
    print(f"PnL ${F.pnl.sum():.2f}; {100*F.pnl.sum()/F.shares.sum():.2f} c/sh; "
          f"return on stake {100*F.pnl.sum()/stake:.1f}%")
    by_lvl = F.groupby("level").agg(n=("pnl", "size"), sh=("shares", "sum"),
                                    win=("won", "mean"), pnl=("pnl", "sum"))
    by_lvl["alpha_c"] = 100 * by_lvl.pnl / by_lvl.sh
    print(by_lvl.round(3).to_string())
    # day-cluster bootstrap on total pnl per day
    by_day = F.groupby("day").pnl.sum()
    rng = np.random.default_rng(17)
    days = by_day.values
    bs = [np.mean(rng.choice(days, len(days))) for _ in range(2000)]
    lo, hi = np.quantile(bs, [0.025, 0.975])
    print(f"per-day PnL mean ${by_day.mean():.2f} day-cluster CI "
          f"[${lo:.2f}, ${hi:.2f}] over {len(by_day)} days; "
          f"negative days {(by_day<0).mean():.2f}")
    top = by_day.sort_values().tail(5)
    print(f"top-5 days: {top.sum()/by_day.sum():.2f} of total; "
          f"best day ${by_day.max():.0f}, worst ${by_day.min():.0f}")


if __name__ == "__main__":
    run(sys.argv[1], use_test="test" in sys.argv)
