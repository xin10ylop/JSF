"""Maker version of the post-2026-08-07 endgame edge.

Gate 1's first read says the favoured side RESTS at ~0.99 and only dips to
0.87-0.92 in the instants around a trade, so a taker sees few fillable
moments. The natural alternative is to be the resting order that those dips
trade against: post a bid on the favoured side and let the flow come.

Hypothesis worth taking seriously here, where it was not before: venue-wide
makers earn -0.09c/share gross because crossing flow is mildly informed.
But post-change, whoever sells the favoured side late in the settle window
is plausibly pricing the OLD contract — uninformed with respect to the rule
that now governs. If so this is the rare case where the passive side is on
the right side of the information.

Fill model, deliberately conservative and identical to the earlier maker
work:
  * one resting bid per market, posted the first time |z| >= zmin inside the
    settle window, at `level`
  * a print STRICTLY THROUGH the level always fills us
  * a print AT the level fills only what is left after `queue_ahead` shares
  * we never get more than the print's own size
  * makers pay no fee on this venue

In Up-token terms:
  favoured Up   -> our bid at L fills on a bid-hit print with p_up <= L
  favoured Down -> our bid at L is an offer on Up at 1-L, and fills on an
                   ask-hit print with p_up >= 1-L
"""
import argparse

import numpy as np
import pandas as pd

from endgame_rollavg import build

FEE = 0.07


def simulate(t, zmin, level_off, queue_ahead, size, era):
    """level_off: how far BELOW the empirical fair we rest our bid."""
    from bot.calib import p_up
    out = []
    for slug, g in t.groupby("slug", sort=False):
        g = g.sort_values("ts")
        gq = g[g.z.abs() >= zmin]
        if not len(gq):
            continue
        first = gq.iloc[0]
        z0 = first.z
        want_up = z0 > 0
        fair = float(p_up(abs(z0)))
        L = round(max(0.02, min(0.98, fair - level_off)), 2)
        after = g[g.ts >= first.ts]
        got = 0.0
        cost = 0.0
        q = queue_ahead
        for r in after.itertuples():
            if got >= size:
                break
            if want_up:
                hit = (not r.is_ask) and r.p_up <= L + 1e-9
                through = (not r.is_ask) and r.p_up < L - 1e-9
            else:
                hit = r.is_ask and r.p_up >= (1 - L) - 1e-9
                through = r.is_ask and r.p_up > (1 - L) + 1e-9
            if not hit:
                continue
            avail = float(r.size)
            if not through:                     # at our level: queue first
                consumed = min(avail, q)
                q -= consumed
                avail -= consumed
            if avail <= 0:
                continue
            take = min(avail, size - got)
            got += take
            cost += take * L
        if got <= 0:
            continue
        won = bool(first.up_win) if want_up else (not bool(first.up_win))
        out.append({"slug": slug, "side": "Up" if want_up else "Dn",
                    "z": z0, "level": L, "fair": fair, "shares": got,
                    "won": float(won), "date": first.date, "coin": first.coin,
                    "pnl": got * (float(won) - L)})
    return pd.DataFrame(out)


def report(d, label, n_mkts):
    if not len(d):
        print(f"  {label:<28} no fills")
        return
    ev = d.pnl.sum() / d.shares.sum()
    g = d.groupby("date").apply(
        lambda x: x.pnl.sum() / x.shares.sum())
    se = g.std(ddof=1) / np.sqrt(len(g)) if len(g) > 2 else np.nan
    print(f"  {label:<28} fills={len(d):>5,}/{n_mkts:<5,} "
          f"({len(d)/max(n_mkts,1):>4.0%})  sh={d.shares.sum():>9,.0f} "
          f"avg_lvl={d.level.mean():.3f} hit={d.won.mean():.3f} "
          f"EV={ev*100:+6.2f}c/sh  day-SE {se*100 if se == se else float('nan'):>5.2f}  "
          f"P&L=${d.pnl.sum():>9,.0f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", nargs="+",
                    default=["btc", "eth", "sol", "xrp", "doge"])
    ap.add_argument("--root", default="data/pmfree_aug")
    ap.add_argument("--zmin", type=float, default=2.0)
    ap.add_argument("--size", type=float, default=100)
    ap.add_argument("--queue", type=float, default=200)
    a = ap.parse_args()

    for era in ("post", "pre"):
        ps = [build(c, a.root, 240, era=era) for c in a.coins]
        ps = [p for p in ps if p is not None]
        if not ps:
            continue
        t = pd.concat(ps, ignore_index=True)
        t = t[t.inside]
        nm = t.slug.nunique()
        print(f"\n=== {era.upper()}-change  ({nm:,} markets with settle-window "
              f"prints, {t.date.nunique()} days) ===")
        for off in (0.02, 0.05, 0.08, 0.12):
            for q in (0.0, a.queue):
                d = simulate(t, a.zmin, off, q, a.size, era)
                report(d, f"bid {off:.2f} below fair, queue {q:.0f}", nm)


if __name__ == "__main__":
    main()
