"""Is each fill-price band pulling its weight, with honest error bars?

The status bucket table shows c/share per band but no uncertainty, and
fills inside one market win or lose together -- so a band that looks dead
(or alive) on 900 fills may be 40 independent bets. This clusters every
band by market and prints the t you can actually act on, then answers the
config question directly: what does the record say total P&L and $/day
would have been under different max_price caps?

Two caveats printed with the output:
  * conditioning on the realized fill price is mildly ex-post -- the bot
    chose these fills under max_price=0.99, and a lower cap would have
    changed later caps/cooldowns slightly. Indicative, not exact.
  * sub-0.50 fills carry the pre-change longshot effect (+2.24c control),
    not only the settlement edge. They are shown separately so neither
    effect hides inside the other.

    python3 src/band_report.py [--since ...]
"""
import argparse
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "src")
import score_paper as sp  # noqa: E402

BANDS = [0.0, 0.3, 0.5, 0.7, 0.85, 0.92, 0.95, 0.98, 1.0]


def cluster(x):
    """Share-weighted c/share and clustered-by-market t for a fill set.

    t is capped at +/-99: when every market in a tiny sample wins, the
    residual variance is ~0 and the raw ratio prints absurdities like
    1.9e16 -- a number that silly reads as a bug, and IS one, in the
    display sense. Anything past 99 carries no more information anyway.
    """
    g = x.groupby("slug").agg(pnl=("pnl", "sum"), sh=("shares", "sum"))
    w = g.sh.sum()
    mu = g.pnl.sum() / w
    se = ((g.pnl - mu * g.sh) ** 2).sum() ** 0.5 / w
    t = mu / se if se > 0 else float("nan")
    if t == t and abs(t) > 99:
        t = float("nan") if len(g) < 5 else (99.0 if t > 0 else -99.0)
    return mu * 100, t, len(g)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None)
    args = ap.parse_args()
    a = sp.parser().parse_args([])
    a.since = args.since
    fills = sp.gather(a)
    if not fills:
        print("no fills")
        return
    d = pd.DataFrame(sp.score_rows(fills))
    d = d[d.status == "scored"]
    if not len(d):
        print("nothing scored")
        return
    span_h = (d.t_us.max() - d.t_us.min()) / 3.6e9
    print(f"{len(d):,} scored fills, {d.slug.nunique()} markets, "
          f"{span_h:.1f}h\n")
    print(f"{'band':>12} {'mkts':>5} {'shares':>9} {'avg px':>7} "
          f"{'hit':>6} {'c/sh':>7} {'t(cl)':>6} {'P&L $':>10}")
    d["band"] = pd.cut(d.px, BANDS)
    for b, x in d.groupby("band", observed=True):
        if not len(x):
            continue
        w = x.shares.sum()
        c, t, n = cluster(x)
        print(f"{str(b):>12} {n:>5} {w:>9,.0f} "
              f"{(x.px * x.shares).sum() / w:>7.4f} "
              f"{(x.won * x.shares).sum() / w:>6.3f} {c:>+7.2f} {t:>+6.2f} "
              f"{x.pnl.sum():>+10.2f}")
    print("\n  |t| < ~2 means that band's sign is not yet distinguishable "
          "from luck.\n")

    # The config question. Each row: keep only fills at px <= cap.
    print(f"{'max_price':>10} {'shares':>9} {'$ total':>10} {'$/day':>8} "
          f"{'c/sh':>7} {'t(cl)':>6} {'capital*':>9}")
    for cap in (0.95, 0.97, 0.98, 0.99):
        x = d[d.px <= cap]
        if not len(x):
            continue
        c, t, _ = cluster(x)
        usd_day = x.pnl.sum() / span_h * 24 if span_h > 0 else float("nan")
        avg_cost = (x.px * x.shares).sum()
        print(f"{cap:>10.2f} {x.shares.sum():>9,.0f} {x.pnl.sum():>+10.2f} "
              f"{usd_day:>+8,.0f} {c:>+7.2f} {t:>+6.2f} {avg_cost:>9,.0f}")
    print("  * capital = sum of px*shares over the window, a proxy for "
          "the bankroll each cap puts at risk.\n")

    # Same table with the longshot bucket removed, so the settlement edge
    # is judged on its own.
    x0 = d[d.px >= 0.5]
    print("excluding px < 0.50 (the pre-change longshot effect):")
    for cap in (0.95, 0.97, 0.98, 0.99):
        x = x0[x0.px <= cap]
        if not len(x):
            continue
        c, t, _ = cluster(x)
        usd_day = x.pnl.sum() / span_h * 24 if span_h > 0 else float("nan")
        print(f"{cap:>10.2f} {x.shares.sum():>9,.0f} {x.pnl.sum():>+10.2f} "
              f"{usd_day:>+8,.0f} {c:>+7.2f} {t:>+6.2f}")

    # Per-SIDE split. The calibration table is asymmetric (favoured side
    # wins 0.888 as Up but only 0.808 as Down at |z|=2.5 on the 3-day
    # sample it was fit on) yet both sides share one zmin and one
    # max_price. If the live record shows the same skew, the Down side
    # needs its own gate; if not, the table baked in a 3-day up-drift.
    print("\nby side (the calib-asymmetry check):")
    for side, x in d.groupby("side"):
        if not len(x):
            continue
        w = x.shares.sum()
        c, t, n = cluster(x)
        print(f"  {side:<5} {len(x):>5} fills {w:>9,.0f} sh  "
              f"avg px {(x.px * x.shares).sum() / w:.4f}  "
              f"hit {(x.won * x.shares).sum() / w:.3f}  "
              f"{c:>+6.2f}c/sh  t={t:>+5.2f}  ({n} mkts)")

    # Day-by-day consistency of the two big high bands: a band can be
    # rescued or condemned by one bad day, and that is worth seeing.
    d["day"] = pd.to_datetime(d.t_us, unit="us", utc=True).dt.strftime(
        "%m-%d")
    hi = d[d.px > 0.95]
    if len(hi):
        print("\npx > 0.95 by day (the volume-dominant region):")
        for day, x in hi.groupby("day"):
            c, t, n = cluster(x)
            print(f"  {day}  {x.shares.sum():>9,.0f} sh  {c:>+6.2f}c/sh "
                  f" t={t:>+5.2f}  ${x.pnl.sum():>+9.2f}  ({n} mkts)")


if __name__ == "__main__":
    main()
