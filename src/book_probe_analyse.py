"""Does padding actually win fills? Measured on the real resting book.

The venue matches a taker against RESTING orders and re-validates after
its 250ms hold. So the kill mechanism is precisely this: we see best
ask A at time t, our order lands at t+rtt, and by then the resting size
at or below our limit may be gone.

This replays that on captured top-of-book snapshots (src/book_probe.py),
which needs no oracle and no money:

    unpadded  limit = A        -- what live sent before the fix
    padded    limit = A + pad  -- what live sends now (value-capped in
                                  the bot; here the pad is the variable)

For every sampled second it asks whether the NEXT snapshot still shows
resting size at or below each limit. That ratio is the venue's own fill
rate, and the difference between the two columns is exactly what the
fix buys.

Reported per side, because the two sides are different worlds: in the
endgame the favourite's ask book empties (nothing to lift at any
limit), while the cheap side stays deep -- and the cheap side is where
the require_edge variant actually trades.

    python3 src/book_probe_analyse.py --books books.jsonl
"""
import argparse
import json
from collections import defaultdict


def load(path):
    rows = []
    for line in open(path):
        try:
            rows.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    return rows


def best(levels):
    """(price, size) of the lowest ask, or (None, 0)."""
    if not levels:
        return None, 0.0
    p, s = min(levels, key=lambda x: x[0])
    return p, s


def executable(row, side):
    """The ladder a BUY of `side` can actually cross, in `side` prices.

    Mirrors bot/state.py::best_ask_dn / depth: the CLOB fills a BUY
    against that outcome's own asks, OR against the OTHER outcome's bids
    by minting a complete set (two buys whose prices sum >= 1). Both are
    real resting orders, so the ladders merge. Scoring only a token's own
    asks makes the favourite look unbuyable whenever nobody happens to be
    offering it -- which is most of the endgame, and was the artifact
    that produced a fake 50% "empty book" rate.
    """
    other = "Down" if side == "Up" else "Up"
    lad = [(p, s) for p, s in (row.get(side) or [])]
    for p, s in (row.get(other + "_bid") or []):
        lad.append((round(1.0 - p, 4), s))
    return sorted(lad)


def depth_at_or_below(levels, limit):
    return sum(s for p, s in (levels or []) if p <= limit + 1e-9)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--books", required=True)
    ap.add_argument("--pads", default="0.0,0.01,0.02,0.05")
    ap.add_argument("--min-rem", type=float, default=2.0)
    ap.add_argument("--max-rem", type=float, default=30.0,
                    help="the live gate trades rem in (2, 30]")
    ap.add_argument("--need", type=float, default=5.0,
                    help="shares required to count as a fill (venue min)")
    ap.add_argument("--rtt", type=float, default=0.4,
                    help="seconds from decision to our order landing")
    a = ap.parse_args()
    pads = [float(x) for x in a.pads.split(",")]
    rows = load(a.books)
    print(f"snapshots: {len(rows)}  markets: "
          f"{len({r['slug'] for r in rows})}")

    # index by (slug) -> ordered snapshots
    by_slug = defaultdict(list)
    for r in rows:
        by_slug[r["slug"]].append(r)
    for v in by_slug.values():
        v.sort(key=lambda r: -r["rem"])

    # band buckets by the price we would be paying
    bands = [(0.0, 0.10), (0.10, 0.30), (0.30, 0.60), (0.60, 0.90),
             (0.90, 1.0)]
    stat = {p: defaultdict(lambda: [0, 0]) for p in pads}   # band->[att,fill]
    empty_side = [0, 0]                                     # [checks, empty]

    rembuckets = [(2, 5), (5, 10), (10, 20), (20, 30)]
    byrem = {p: defaultdict(lambda: [0, 0]) for p in pads}
    for slug, snaps in by_slug.items():
        for i in range(len(snaps) - 1):
            cur = snaps[i]
            # the snapshot at our ACTUAL round trip, not merely the next
            # one: sampling cadence must not decide what we measure
            nxt = None
            for j in range(i + 1, len(snaps)):
                if cur["rem"] - snaps[j]["rem"] >= a.rtt:
                    nxt = snaps[j]
                    break
            if nxt is None:
                continue
            if not (a.min_rem <= cur["rem"] <= a.max_rem):
                continue
            rb = next((r for r in rembuckets
                       if r[0] <= cur["rem"] < r[1]), None)
            for side in ("Up", "Down"):
                lv_now = executable(cur, side)
                lv_next = executable(nxt, side)
                empty_side[0] += 1
                if not lv_now:
                    empty_side[1] += 1
                    continue
                A, _sz = best(lv_now)
                if A is None or not (0 < A < 1):
                    continue
                band = next((b for b in bands if b[0] <= A < b[1]), None)
                if band is None:
                    continue
                for pad in pads:
                    limit = min(A + pad, 0.99)
                    got = depth_at_or_below(lv_next, limit)
                    hit = got >= a.need
                    st = stat[pad][band]
                    st[0] += 1
                    st[1] += hit
                    if rb:
                        rs = byrem[pad][rb]
                        rs[0] += 1
                        rs[1] += hit

    print(f"\nside-snapshots with NO executable liquidity (own asks AND mirror): "
          f"{empty_side[1]}/{empty_side[0]} "
          f"({100 * empty_side[1] / max(1, empty_side[0]):.0f}%) "
          f"-- no limit of any size can lift an empty book")

    print(f"\nfill rate = resting size >= {a.need:.0f}sh at or below our "
          f"limit, one snapshot later (rem {a.min_rem:.0f}-{a.max_rem:.0f}s)")
    hdr = "  ask band      n  " + "".join(f"  pad{100 * p:>2.0f}c" for p in pads)
    print(hdr)
    for band in bands:
        n = stat[pads[0]][band][0]
        if not n:
            continue
        cells = ""
        for p in pads:
            att, fil = stat[p][band]
            cells += f"   {100 * fil / att:>5.0f}%" if att else "       -"
        print(f"  {band[0]:.2f}-{band[1]:.2f} {n:>6}{cells}")
    print("\n  by time-to-expiry (uniform rates => not conditioning on the "
          "strategy's exact firing second is harmless)")
    print("  rem       n  " + "".join(f"  pad{100 * p:>2.0f}c" for p in pads))
    for rb in rembuckets:
        n = byrem[pads[0]][rb][0]
        if not n:
            continue
        cells = ""
        for p in pads:
            att, fil = byrem[p][rb]
            cells += f"   {100 * fil / att:>5.0f}%" if att else "       -"
        print(f"  {rb[0]:>2}-{rb[1]:<3}{n:>7}{cells}")
    tot = {p: [sum(v[0] for v in stat[p].values()),
               sum(v[1] for v in stat[p].values())] for p in pads}
    cells = "".join(f"   {100 * tot[p][1] / tot[p][0]:>5.0f}%"
                    if tot[p][0] else "       -" for p in pads)
    print(f"  {'ALL':<9} {tot[pads[0]][0]:>6}{cells}")


if __name__ == "__main__":
    main()
