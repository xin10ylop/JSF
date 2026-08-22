"""Is the post-open price move predictable, and worth trading?

Answers three questions in order, so the idea can die cheaply:

1. CEILING -- with perfect foresight of the price at t+X, what does the
   trade earn? Entry is a REAL print near the open, exit is a REAL
   print near t+X, both net of the venue's 0.07*p*(1-p) taker fee. No
   predictor can beat this, so a small ceiling ends the discussion.

2. SIGNALS -- do simple predictors an LLM would also see (BTC momentum
   over several lookbacks, the open price's own skew) beat a coin
   flip? If they score zero, a model reading the same candles has no
   evident advantage; if one works, it runs for free.

3. EXITS -- a resting sell at a target versus holding to settlement,
   scored on the same trades. A sell limit only fills when the price
   rises THROUGH it, so it caps winners while keeping every loser
   whole; this measures that cost rather than assuming it.
"""
import argparse
import glob
import json
import math
import statistics

CACHE = ("/tmp/claude-0/-home-user-JSF/"
         "1e3a1627-7b11-57c3-a9bf-dff6bca1dab2/scratchpad/openjump")
FEE = lambda p: 0.07 * p * (1 - p)          # noqa: E731


def load(coin="btc"):
    out = []
    for f in sorted(glob.glob(f"{CACHE}/oj_{coin}*.jsonl")):
        for line in open(f):
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            r["path"] = {int(k): v + r["base"] for k, v in r["path"].items()}
            out.append(r)
    return out


def px_at(prints, lo, hi):
    """First print in [lo, hi] as an Up-price, or None."""
    for ts, up, sz, side in prints:
        if lo <= ts <= hi:
            return up
    return None


def px_last(prints, lo, hi):
    got = None
    for ts, up, sz, side in prints:
        if lo <= ts <= hi:
            got = up
    return got


def stats(v):
    v = [x for x in v if x is not None]
    n = len(v)
    if n < 2:
        return n, 0.0, 0.0
    m = sum(v) / n
    sd = statistics.pstdev(v)
    return n, 100 * m, (m / (sd / math.sqrt(n)) if sd > 0 else 0.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", default="btc")
    ap.add_argument("--target", type=float, default=0.60)
    a = ap.parse_args()
    recs = load(a.coin)
    print(f"{a.coin}: {len(recs)} markets\n")

    # entry: first real print in the opening 5s; the study's premise is
    # buying near 0.50, so keep entries in a band around it
    usable = []
    for r in recs:
        e = px_at(r["prints"], r["t0"], r["t0"] + 5)
        if e is None or not (0.35 <= e <= 0.65):
            continue
        usable.append((r, e))
    print(f"entries near 0.50 in the first 5s: {len(usable)} "
          f"({100*len(usable)/max(1,len(recs)):.0f}% of markets)\n")

    print("1) CEILING -- perfect foresight of the t+X price")
    print(f"   {'horizon':>8}{'n':>6}{'EV c/sh':>10}{'t':>7}   (best case, "
          f"no model can beat this)")
    for h in (10, 20, 30):
        evs = []
        for r, e in usable:
            xp = px_last(r["prints"], r["t0"] + h - 5, r["t0"] + h + 5)
            if xp is None:
                continue
            # perfect foresight: take whichever side gains
            up_pnl = (xp - e) - FEE(e)
            dn_pnl = ((1 - xp) - (1 - e)) - FEE(1 - e)
            evs.append(max(up_pnl, dn_pnl))
        n, ev, t = stats(evs)
        print(f"   t+{h:<6}{n:>6}{ev:>+10.2f}{t:>+7.1f}")

    print("\n2) SIGNALS -- can anything pick the side at t-10?")
    print(f"   {'predictor':<22}{'horizon':>8}{'n':>6}{'hit%':>7}"
          f"{'EV c/sh':>10}{'t':>7}")
    for lb in (10, 30, 60):
        for h in (10, 20, 30):
            evs, hits = [], []
            for r, e in usable:
                p0, pm = r["path"].get(0), r["path"].get(-lb)
                if p0 is None or pm is None or pm <= 0:
                    continue
                mom = p0 - pm
                if mom == 0:
                    continue
                side_up = mom > 0
                xp = px_last(r["prints"], r["t0"] + h - 5, r["t0"] + h + 5)
                if xp is None:
                    continue
                entry = e if side_up else 1 - e
                exit_ = xp if side_up else 1 - xp
                evs.append((exit_ - entry) - FEE(entry))
                hits.append(1 if exit_ > entry else 0)
            n, ev, t = stats(evs)
            hr = 100 * sum(hits) / len(hits) if hits else 0
            print(f"   {'BTC mom ' + str(lb) + 's':<22}t+{h:<6}{n:>6}"
                  f"{hr:>6.0f}%{ev:>+10.2f}{t:>+7.1f}")

    print(f"\n3) EXITS -- resting sell at {a.target:.2f} vs holding to "
          f"settlement (momentum-30s side)")
    for label in ("sell_limit", "hold"):
        evs = []
        for r, e in usable:
            p0, pm = r["path"].get(0), r["path"].get(-30)
            if p0 is None or pm is None or pm <= 0 or p0 == pm:
                continue
            side_up = p0 > pm
            entry = e if side_up else 1 - e
            if label == "sell_limit":
                hit = False
                for ts, up, sz, side in r["prints"]:
                    if ts <= r["t0"]:
                        continue
                    q = up if side_up else 1 - up
                    if q >= a.target:
                        hit = True
                        break
                if hit:
                    evs.append((a.target - entry) - FEE(entry))
                    continue
            won = r["up_won"] if side_up else not r["up_won"]
            evs.append(((1.0 if won else 0.0) - entry) - FEE(entry))
        n, ev, t = stats(evs)
        print(f"   {label:<22}{n:>6}{ev:>+10.2f}{t:>+7.1f}")


if __name__ == "__main__":
    main()
