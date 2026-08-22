"""Can the jump-scalp actually get in and out, on the real book?

The tape study says +2.07c/share out-of-sample with a taker exit. It
assumes two things about liquidity that prints cannot prove:

  ENTRY  buying near 0.50 in the first seconds needs resting ASK size
         at or below our limit at the moment we land.
  EXIT   crossing out at t+30 needs resting BID size to sell into --
         a print says a trade happened, not that depth existed for us.

This measures both against captured top-of-book (src/book_probe.py
--phase open), the same instrument that overturned an earlier capacity
claim built from prints.

Executable price uses the mirror, as bot/state.py does: a BUY of one
outcome crosses that outcome's asks OR the other outcome's bids by
minting a complete set. Scoring only a token's own book understates
what is reachable.

    python3 src/openbook_analyse.py --books data/openbook/open.jsonl
"""
import argparse
import json
from collections import defaultdict


def executable_ask(row, side):
    """Ladder a BUY of `side` can cross, cheapest first."""
    other = "Down" if side == "Up" else "Up"
    lad = [(p, s) for p, s in (row.get(side) or [])]
    for p, s in (row.get(other + "_bid") or []):
        lad.append((round(1.0 - p, 4), s))
    return sorted(lad)


def executable_bid(row, side):
    """Ladder a SELL of `side` can hit, richest first."""
    other = "Down" if side == "Up" else "Up"
    lad = [(p, s) for p, s in (row.get(side + "_bid") or [])]
    for p, s in (row.get(other) or []):
        lad.append((round(1.0 - p, 4), s))
    return sorted(lad, reverse=True)


def depth_at_or_below(lad, limit):
    return sum(s for p, s in lad if p <= limit + 1e-9)


def depth_at_or_above(lad, floor):
    return sum(s for p, s in lad if p >= floor - 1e-9)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--books", required=True)
    ap.add_argument("--entry-by", type=float, default=5.0,
                    help="seconds after open we must be filled by")
    ap.add_argument("--exit-at", type=float, default=30.0)
    ap.add_argument("--target", type=float, default=0.09)
    ap.add_argument("--need", type=float, default=50.0,
                    help="shares we want to trade")
    a = ap.parse_args()
    rows = [json.loads(x) for x in open(a.books)]
    by = defaultdict(list)
    for r in rows:
        by[r["slug"]].append(r)
    for v in by.values():
        v.sort(key=lambda r: r["since_open"])
    print(f"snapshots {len(rows)}  markets {len(by)}\n")

    ent_ok = ent_tot = 0
    ex_ok = ex_tot = 0
    ent_depths, ex_depths = [], []
    for slug, snaps in by.items():
        for side in ("Up", "Down"):
            # ENTRY: first snapshot inside the entry window
            e = next((s for s in snaps if s["since_open"] <= a.entry_by), None)
            if e is None:
                continue
            lad = executable_ask(e, side)
            if not lad:
                continue
            best = lad[0][0]
            if not (0.30 <= best <= 0.70):
                continue          # the strategy only enters near 0.50
            d = depth_at_or_below(lad, best + 0.01)
            ent_tot += 1
            ent_depths.append(d)
            if d >= a.need:
                ent_ok += 1
            # EXIT: snapshot nearest the exit horizon
            x = min(snaps, key=lambda s: abs(s["since_open"] - a.exit_at))
            if abs(x["since_open"] - a.exit_at) > 5:
                continue
            bl = executable_bid(x, side)
            if not bl:
                continue
            ex_tot += 1
            db = depth_at_or_above(bl, bl[0][0] - 0.01)
            ex_depths.append(db)
            if db >= a.need:
                ex_ok += 1

    def q(v, f):
        v = sorted(v)
        return v[min(len(v) - 1, int(f * len(v)))] if v else 0

    print(f"ENTRY (buy near 0.50 within {a.entry_by:.0f}s of open)")
    print(f"  sides checked {ent_tot}   "
          f"could fill {a.need:.0f}sh: {100*ent_ok/max(1,ent_tot):.0f}%")
    print(f"  executable ask depth: p10={q(ent_depths,.1):.0f} "
          f"med={q(ent_depths,.5):.0f} p90={q(ent_depths,.9):.0f} sh\n")
    print(f"EXIT (cross out at t+{a.exit_at:.0f}s)")
    print(f"  sides checked {ex_tot}   "
          f"could sell {a.need:.0f}sh: {100*ex_ok/max(1,ex_tot):.0f}%")
    print(f"  executable bid depth: p10={q(ex_depths,.1):.0f} "
          f"med={q(ex_depths,.5):.0f} p90={q(ex_depths,.9):.0f} sh")


if __name__ == "__main__":
    main()
