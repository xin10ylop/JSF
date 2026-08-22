"""Scalp the opening jump. Predict the MOVE, never the outcome.

Buy near 0.50 in the first seconds of a 5m market, then leave within a
minute -- at a profit target if the price gets there, otherwise flat at
t+N. Settlement risk is never carried, so the binary payoff asymmetry
that makes 0.99 entries lethal never applies.

Why it might exist: at open the strike is already fixed (the mean over
the 30s BEFORE t0), so distance-to-strike is known and governs how far
the price can travel. Near the strike small spot ticks swing the
probability hard; far away the price is pinned. Measured: the price
ranges ~22c in the first 30s and touches +9c about 60% of the time.

Exits are modelled two ways because the difference is the whole
question:
  taker  cross the spread to get out -- pay a tick, always fills
  maker  rest the sell and only fill when a print goes THROUGH it,
         then bail at t+N at the taker price if it never does

Discipline: parameters are pre-committed at the top, the sweep is
reported separately, and every cell is split fit/holdout by time.

    python3 src/jumpscalp_lab.py --coin btc
"""
import argparse
import glob
import json
import math
import statistics

CACHE = ("/tmp/claude-0/-home-user-JSF/"
         "1e3a1627-7b11-57c3-a9bf-dff6bca1dab2/scratchpad/openjump")

# --- pre-committed rule, fixed BEFORE looking at the holdout ---------
ZMIN = 0.15          # the 0.00-0.15 bucket was a coin flip and lost
TARGET = 0.09        # +9c, the move the price makes ~60% of the time
EXIT_S = 30          # bail flat at t+30 if the target never prints
MOM_S = 10           # 10s BTC momentum picked the side; 30s/60s did not
TICK = 0.01          # crossing the spread on the way out
FEE = lambda p: 0.07 * p * (1 - p)                       # noqa: E731


def load(coin):
    out = []
    for f in sorted(glob.glob(f"{CACHE}/oj_{coin}.jsonl")):
        for line in open(f):
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            r["path"] = {int(k): v + r["base"] for k, v in r["path"].items()}
            out.append(r)
    return out


def zscore(r):
    """Distance to the already-fixed strike, in sds of what is left."""
    p, t0 = r["path"], r["t0"]
    k = [p[u] for u in range(-30, 0) if u in p]
    if len(k) < 20:
        return None
    K = sum(k) / len(k)
    spot = p.get(0) or p.get(-1)
    if not spot:
        return None
    xs = sorted(u for u in p if -130 <= u < 0)
    rs = [math.log(p[b] / p[a]) for a, b in zip(xs, xs[1:])
          if b - a == 1 and p[a] > 0]
    if len(rs) < 60:
        return None
    sd = statistics.pstdev(rs)
    if sd <= 0:
        return None
    return (spot - K) / (sd * spot * math.sqrt(270 + 10))


def trades(recs, zmin=ZMIN, target=TARGET, exit_s=EXIT_S, mom_s=MOM_S,
           exit_style="maker", entry_band=(0.30, 0.70)):
    out = []
    for r in recs:
        z = zscore(r)
        if z is None or abs(z) < zmin:
            continue
        p, t0 = r["path"], r["t0"]
        p0, pm = p.get(0), p.get(-mom_s)
        if p0 is None or pm is None or p0 == pm:
            continue
        side_up = p0 > pm                       # BTC just ticked up
        entry_up = None
        for ts, up, sz, sd_ in r["prints"]:     # first real print at open
            if t0 <= ts <= t0 + 5:
                entry_up = up
                break
        if entry_up is None:
            continue
        entry = entry_up if side_up else 1 - entry_up
        if not (entry_band[0] <= entry <= entry_band[1]):
            continue
        tgt = entry + target
        # walk prints forward to the exit horizon
        hit, last = False, None
        for ts, up, sz, sd_ in r["prints"]:
            if not (t0 < ts <= t0 + exit_s):
                continue
            q = up if side_up else 1 - up
            last = q
            if q >= tgt:
                hit = True
                break
        if last is None:
            continue
        if exit_style == "taker":
            # cross the spread immediately at the horizon price
            exit_px = min(last, tgt) - TICK
            pnl = exit_px - entry - FEE(entry)
        else:
            # rest the sell; if it never prints through, bail at t+N
            if hit:
                pnl = tgt - entry - FEE(entry)   # maker exit, no exit fee
            else:
                pnl = (last - TICK) - entry - FEE(entry)
        out.append({"t0": t0, "coin": r["coin"], "z": z, "entry": entry,
                    "hit": hit, "pnl": pnl})
    return out


def rep(tag, ts):
    n = len(ts)
    if n < 2:
        print(f"   {tag:<26}{n:>5}  (too few)")
        return
    v = [x["pnl"] for x in ts]
    m = sum(v) / n
    sd = statistics.pstdev(v)
    t = m / (sd / math.sqrt(n)) if sd > 0 else 0
    sold = 100 * sum(1 for x in ts if x["hit"]) / n
    ent = sum(x["entry"] for x in ts) / n
    print(f"   {tag:<26}{n:>5}{100*m:>+9.2f}{t:>+7.2f}{sold:>7.0f}%"
          f"{ent:>8.3f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", default="btc")
    ap.add_argument("--sweep", action="store_true")
    a = ap.parse_args()
    recs = load(a.coin)
    if not recs:
        print("no data")
        return
    lo, hi = min(r["t0"] for r in recs), max(r["t0"] for r in recs)
    cut = lo + (hi - lo) * 0.6
    print(f"{a.coin}: {len(recs)} markets, "
          f"{(hi-lo)/86400:.1f} days  (fit 60% / holdout 40%)\n")
    print(f"PRE-COMMITTED: |z|>={ZMIN}, target +{100*TARGET:.0f}c, "
          f"exit t+{EXIT_S}s, {MOM_S}s momentum, never held to settle")
    print(f"   {'':<26}{'n':>5}{'EV c/sh':>9}{'t':>7}{'sold':>8}{'entry':>8}")
    for style in ("maker", "taker"):
        ts = trades(recs, exit_style=style)
        rep(f"{style} exit  ALL", ts)
        rep(f"{style} exit  fit", [x for x in ts if x["t0"] < cut])
        rep(f"{style} exit  HOLDOUT", [x for x in ts if x["t0"] >= cut])
    if not a.sweep:
        return
    print("\nSWEEP (maker exit) -- reported separately, not the rule")
    print(f"   {'zmin/target/exit':<26}{'n':>5}{'EV c/sh':>9}{'t':>7}"
          f"{'sold':>8}{'entry':>8}")
    for zmin in (0.0, 0.15, 0.30):
        for tg in (0.05, 0.09, 0.15):
            for ex in (15, 30, 60):
                ts = trades(recs, zmin=zmin, target=tg, exit_s=ex)
                rep(f"z{zmin} +{100*tg:.0f}c t+{ex}", ts)


if __name__ == "__main__":
    main()
