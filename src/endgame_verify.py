"""Verify the endgame gate out-of-sample and size its risk from data.

Everything the 2026-08-22 re-verification found, runnable in one pass so
the next tuning argument is settled by re-running it rather than by
memory:

  1  the deployed gate, fit vs a 40% TIME holdout (never tuned on)
  2  every lever -- zmin, require_edge, min_rem, tick pad -- judged on
     the holdout, because zmin 2.5/3.0 and require_edge all look BETTER
     in-sample and all FAIL out-of-sample; picking on fit alone is how
     this strategy would quietly overfit itself
  3  the honest dollar simulation: fills capped by the size of the
     print that actually traded (a naive cap-refill model read
     $545/day; the print-capped floor is ~$10-50/day -- capacity, not
     edge, is the binding constraint)
  4  daily sd, worst day, and share-size scaling, which is what the
     daily stop and per-market cap must be derived from

    python3 src/endgame_verify.py
"""
import sys
import time

sys.path.insert(0, "src")
from endgame_lab import load, replay, stats     # noqa: E402


def run(recs, cut, tag, **kw):
    rows = replay(recs, **kw)
    parts = []
    for name, rs in (("fit", [r for r in rows if r["t0"] < cut]),
                     ("HOLD", [r for r in rows if r["t0"] >= cut])):
        v = [r["pnl_fwd"] for r in rs if r["pnl_fwd"] is not None]
        n, ev, t = stats(v)
        parts.append(f"{name} n={n:>4} {ev:>+6.2f}c(t{t:>+5.1f})")
    print(f"  {tag:<26} {' | '.join(parts)}")
    return rows


def sim(rows, size, cap, tag):
    byday = {}
    for r in rows:
        if r["pnl_fwd"] is None or not r.get("sz"):
            continue
        sh = min(size, r["sz"], cap / r["px_fwd"])
        d = time.strftime("%m-%d", time.gmtime(r["t0"]))
        byday.setdefault(d, []).append(sh * r["pnl_fwd"])
    daily = [sum(v) for v in byday.values()]
    m = sum(daily) / len(daily)
    sd = (sum((x - m) ** 2 for x in daily) / max(1, len(daily) - 1)) ** .5
    neg = sum(1 for x in daily if x < 0)
    print(f"  {tag:<24} ${m:>+7.2f}/day  sd ${sd:>6.2f}  "
          f"worst ${min(daily):>+7.2f}  days- {neg}/{len(daily)}")


def main():
    recs = load("btc")
    if not recs:
        print("no tape -- run src/endgame_fetch.py first")
        return
    lo = min(r["t0"] for r in recs)
    hi = max(r["t0"] for r in recs)
    cut = lo + (hi - lo) * 0.6
    print(f"{len(recs)} markets, {(hi-lo)/86400:.1f} days, "
          f"fit 60% / holdout 40%\n")
    print("1) GATE, fit vs holdout")
    base = run(recs, cut, "z2.0 (old live)", zmin=2.0, max_price=0.99)
    z15 = run(recs, cut, "z1.5 (deployed)", zmin=1.5, max_price=0.99)
    print("\n2) LEVERS -- judged on HOLD only")
    for zm in (2.5, 3.0):
        run(recs, cut, f"zmin {zm} (overfits)", zmin=zm, max_price=0.99)
    run(recs, cut, "require_edge (overfits)", zmin=2.0, max_price=0.99,
        require_edge=True)
    run(recs, cut, "+2 tick pad", zmin=2.0, max_price=0.99, tick=0.02)
    print("\n3) HONEST $ SIM -- fills capped by the traded print")
    sim(z15, 15, 15, "size 15/$15 (deployed)")
    sim(z15, 25, 25, "size 25/$25")
    sim(z15, 50, 50, "size 50/$50")
    print("\n4) CAPACITY -- how much of an ask actually fills")
    for want in (15, 50, 100, 250):
        got = [min(want, r["sz"]) for r in base
               if r["pnl_fwd"] is not None and r.get("sz")]
        print(f"  ask {want:>3} sh -> avg {sum(got)/len(got):>5.1f} sh "
              f"({100*sum(got)/(want*len(got)):.0f}%)")
    print("\nfloor estimates: one print per market; live re-fires can "
          "catch several, so reality sits above these numbers.")


if __name__ == "__main__":
    main()
