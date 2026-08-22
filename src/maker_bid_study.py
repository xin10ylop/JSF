"""Can we buy the locked band cheaper by RESTING a bid instead of lifting?

The endgame takes the ask, and in a locked market that ask is 0.98-0.99
where a win pays a cent or two. A fill at 0.96-0.97 would double or
triple the edge per share. The question is whether the seller who hits
our bid knows something we don't.

There is history here: ZMaker rested bids through the whole market and
lost 22.8c/share, because a bid left out all window fills exactly when
the outcome turns against it. This tests a narrower claim -- a bid
placed ONLY after z locks, ONLY in the settle window, ONLY in the band
where the model is already 99% confident. At that point a seller at
0.96 is plausibly someone taking profit early rather than someone with
information.

Method, print-proof throughout:

  place   first second in [t1-window, t1-min_rem] where |z| >= zmin and
          the favoured side trades at or above lo_px. Bid at
          (prevailing - offset), rounded to the cent grid.
  fill    ONLY where a real SELL print on our side crossed the bid,
          at least `lat` after we placed it. `strict` requires the
          print to go THROUGH our price (we are last in the queue at
          our own level); `touch` counts a print at our price.
  score   held to settlement, no venue fee -- a resting bid that gets
          hit is a maker fill.

The comparison that matters is against TAKING on the same
opportunities, so both columns are computed on the identical set.

    python3 src/maker_bid_study.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from endgame_lab import load, prep, z_at, stats     # noqa: E402

W = 30.0
FEE = 0.07


def study(recs, zmin=1.5, offset=0.02, lo_px=0.90, window_s=60,
          min_rem_s=2.0, lat=0.4, strict=True):
    """Returns (n_opportunities, maker_rows, taker_rows).

    maker_rows and taker_rows are the SAME opportunities scored two
    ways, so the comparison is like-for-like rather than two different
    trade populations.
    """
    opps = 0
    maker, taker = [], []
    for r in recs:
        pr = prep(r, W)
        if pr is None:
            continue
        path, K = pr
        t0, t1 = r["t0"], r["t1"]
        prints = r["prints"]
        placed = None
        for te in range(int(t1 - window_s), int(t1 - min_rem_s)):
            z = z_at(path, K, te, t0, t1, r["sigma"], W)
            if z is None or abs(z) < zmin:
                continue
            side_up = z > 0
            back = [p for p in prints if p[0] <= te]
            if not back:
                continue
            q = back[-1][1] if side_up else 1 - back[-1][1]
            if not (lo_px <= q <= 0.995):
                continue
            placed = (te, side_up, round(q - offset, 2), q)
            break
        if placed is None:
            continue
        te, side_up, bid, ask = placed
        opps += 1
        won = r["up_won"] if side_up else not r["up_won"]
        payoff = 1.0 if won else 0.0
        # taker baseline: lift the ask we saw, pay the fee
        taker.append({"pnl": payoff - ask - FEE * ask * (1 - ask),
                      "px": ask, "won": won})
        want = "up" if side_up else "down"
        for ts, upx, sz, sd_, oc in prints:
            if ts < te + lat or ts > t1:
                continue
            if sd_ != "SELL" or oc != want:
                continue
            p = upx if side_up else 1 - upx
            hit = (p < bid - 1e-9) if strict else (p <= bid + 1e-9)
            if hit:
                # maker fill: no venue fee
                maker.append({"pnl": payoff - bid, "px": bid,
                              "won": won, "sz": sz})
                break
    return opps, maker, taker


def row(tag, opps, rows, extra=""):
    if len(rows) < 2:
        print(f"  {tag:<22}{opps:>6}{len(rows):>7}   (too few fills)")
        return
    n, ev, t = stats([x["pnl"] for x in rows])
    w = 100 * sum(1 for x in rows if x["won"]) / n
    px = sum(x["px"] for x in rows) / n
    print(f"  {tag:<22}{opps:>6}{n:>7}{100*n/max(1,opps):>6.0f}%"
          f"{ev:>+9.2f}{t:>+7.1f}{w:>6.0f}%{px:>8.3f}{extra}")


def main():
    recs = load("btc")
    if not recs:
        print("no tape -- run src/endgame_fetch.py first")
        return
    print(f"{len(recs)} btc markets, print-proof, locked band only "
          f"(>=0.90)\n")
    print(f"  {'rule':<22}{'opps':>6}{'fills':>7}{'fill%':>7}"
          f"{'EV c/sh':>9}{'t':>7}{'win%':>6}{'avg px':>8}")
    base_done = False
    for off in (0.01, 0.02, 0.03):
        for strict in (True, False):
            opps, mk, tk = study(recs, offset=off, strict=strict)
            if not base_done and tk:
                row("TAKE the ask (now)", opps, tk)
                print()
                base_done = True
            row(f"rest at ask-{off:.2f} "
                f"{'strict' if strict else 'touch'}", opps, mk)
    print("\n  strict = the print must go THROUGH our price (we are last")
    print("  in the queue). touch = a print at our price fills us.")
    print("  A maker fill pays no venue fee; the taker row includes it.")


if __name__ == "__main__":
    main()
