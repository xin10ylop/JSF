"""Measure the scalp AS THE LIVE BOT TRADES IT: decide before the open.

src/jumpscalp_lab.py scores an at-open entry. The deployed config enters
at t-12..t-2, and its trades() picks the side from `p.get(0)` -- the spot
AT THE OPEN -- while claiming to decide earlier. That is look-ahead of up
to 12 seconds on the direction signal, the same class of error that once
turned a true +2.78c/sh into a fictional +5.45c here.

Everything below is evaluated strictly at the decision instant `te`:

  z        zscore(r, te), which already truncates the strike and spot
  side     momentum over [te-mom_s, te], both samples at or before te
  entry    the first PRINT at or after te -- we cannot be filled by a
           trade that happened before we decided
  exit     target if the price prints through it within exit_s of ENTRY,
           otherwise the last print at the horizon, minus a tick

Reported fit/holdout by time, and swept over te so the cost of deciding
early is visible rather than assumed.

    python3 src/jumpscalp_preopen.py --coin btc
"""
import argparse
import math
import statistics

from jumpscalp_lab import CACHE, FEE, TICK, load, zscore   # noqa: F401

ZMIN = 0.15
TARGET = 0.09
EXIT_S = 30
MOM_S = 10
BAND = (0.30, 0.70)


def spot_at(path, u):
    """Last path sample at or before u -- never after."""
    for k in range(int(u), int(u) - 6, -1):
        if k in path:
            return path[k]
    return None


def trades(recs, te, zmin=ZMIN, target=TARGET, exit_s=EXIT_S,
           mom_s=MOM_S, dir_mode="both", band=BAND, exit_style="maker",
           lag_s=0.0):
    out = []
    for r in recs:
        z = zscore(r, te)
        if z is None or abs(z) < zmin:
            continue
        p, t0 = r["path"], r["t0"]
        now_px = spot_at(p, te)
        back_px = spot_at(p, te - mom_s)
        if now_px is None or back_px is None:
            continue
        mom = now_px - back_px
        if dir_mode == "both":
            if mom == 0 or (mom > 0) != (z > 0):
                continue
            side_up = z > 0
        elif dir_mode == "z":
            side_up = z > 0
        else:
            if mom == 0:
                continue
            side_up = mom > 0

        # entry: first print at or AFTER the decision instant
        # lag_s models the gap between deciding and being filled: signing
        # and submitting takes ~400ms live, and an at-open entry that
        # assumes a zero-latency fill on the very first print is the
        # flattering case. A real edge should survive a second of it.
        t_dec = t0 + te + lag_s
        entry_up = entry_ts = None
        for ts, up, _sz, _sd in r["prints"]:
            if ts >= t_dec:
                entry_up, entry_ts = up, ts
                break
        if entry_up is None or entry_ts > t0 + 5:
            continue                      # no fill within the entry window
        entry = entry_up if side_up else 1 - entry_up
        if not (band[0] <= entry <= band[1]):
            continue

        tgt = entry + target
        # A resting sell at tgt is at the BACK of the queue at that price.
        # A print exactly AT tgt may be filling someone ahead of us, so
        # "maker_strict" only counts a print that goes THROUGH the level.
        # The difference is the whole ZMaker lesson (-22.8c/share on
        # assumed queue position), so it is measured, not assumed.
        strict = exit_style == "maker_strict"
        hit, last, hit_px = False, None, None
        for ts, up, _sz, _sd in r["prints"]:
            if not (entry_ts < ts <= entry_ts + exit_s):
                continue
            q = up if side_up else 1 - up
            last = q
            if (q > tgt + 1e-9) if strict else (q >= tgt):
                hit, hit_px = True, q
                break
        if last is None:
            continue
        # maker exit: the resting sell at the target is LIFTED, so no tick
        # and no exit fee -- this is the design the live bot implements.
        # A miss still has to cross out at the horizon.
        if exit_style in ("maker", "maker_strict") and hit:
            pnl = tgt - entry - FEE(entry)
        elif exit_style == "taker_ride":
            # What the live bot ACTUALLY does: poll the book, and the
            # moment the bid is at or above the target, cross. It fills
            # at the BID, which can be well past the target -- observed
            # live at 0.74/0.67/0.69 against 9c targets. Capping the
            # taker at tgt (as the other styles do) understates it and
            # made the resting sell look strictly better than it is.
            px = hit_px if hit else last
            pnl = (px - TICK) - entry - FEE(entry) - FEE(px)
        else:
            pnl = ((tgt if hit else last) - TICK) - entry - FEE(entry)
        out.append({"t0": t0, "z": z, "entry": entry, "hit": hit,
                    "up": last > entry, "pnl": pnl})
    return out


def rep(tag, ts):
    n = len(ts)
    if n < 2:
        print(f"   {tag:<22}{n:>6}  (too few)")
        return
    v = [x["pnl"] for x in ts]
    m = sum(v) / n
    sd = statistics.pstdev(v)
    t = m / (sd / math.sqrt(n)) if sd > 0 else 0
    print(f"   {tag:<22}{n:>6}{100*m:>+10.2f}{t:>+7.2f}"
          f"{100*sum(x['hit'] for x in ts)/n:>7.0f}%"
          f"{100*sum(x['up'] for x in ts)/n:>7.0f}%"
          f"{sum(x['entry'] for x in ts)/n:>8.3f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", default="btc")
    a = ap.parse_args()
    recs = load(a.coin)
    if not recs:
        print("no data")
        return
    lo, hi = min(r["t0"] for r in recs), max(r["t0"] for r in recs)
    cut = lo + (hi - lo) * 0.6
    days = (hi - lo) / 86400
    print(f"{a.coin}: {len(recs)} markets over {days:.1f} days "
          f"(fit 60% / holdout 40%)")
    print(f"rule: |z|>={ZMIN}, side = z AND {MOM_S}s momentum agreeing, "
          f"entry {BAND[0]}-{BAND[1]}, +{100*TARGET:.0f}c or flat at "
          f"t+{EXIT_S}s\n")
    hdr = (f"   {'decide at':<22}{'n':>6}{'EV c/sh':>10}{'t':>7}"
           f"{'sold':>8}{'ended up':>7}{'entry':>8}")
    for style in ("maker", "taker"):
        print(f"\n=== {style} exit "
              f"({'resting sell lifted at the target' if style == 'maker' else 'cross out every time'}) ===")
        print(hdr)
        for te in (-12, -8, -4, -2, 0):
            ts = trades(recs, te, exit_style=style)
            rep(f"t{te:+d}  ALL", ts)
            rep(f"t{te:+d}  holdout", [x for x in ts if x["t0"] >= cut])
            n = len(ts)
            print(f"   {'':<22}{'':>6}   -> {n/max(days,1e-9):.0f}/day, "
                  f"{100*n/len(recs):.0f}% of markets")

    # Friction is the whole argument at these prices: the venue fee
    # 0.07*p*(1-p) is MAXIMISED at p=0.5, which is exactly where this
    # strategy trades, and near-zero where the endgame strategy trades.
    print("\nLATENCY ROBUSTNESS (maker exit, holdout only)")
    print(f"   {'decide/lag':<22}{'n':>6}{'EV c/sh':>10}{'t':>7}"
          f"{'sold':>8}{'ended up':>7}{'entry':>8}")
    for te in (0, -4):
        for lag in (0.0, 1.0, 2.0, 3.0):
            ts = [x for x in trades(recs, te, lag_s=lag) if x["t0"] >= cut]
            rep(f"t{te:+d}  +{lag:.0f}s lag", ts)

    print("\nFRICTION BY ENTRY PRICE (round trip, taker in / maker out)")
    print(f"   {'entry':>7}{'fee c/sh':>10}{'+tick':>8}{'total':>8}")
    for p in (0.50, 0.55, 0.70, 0.90, 0.97, 0.99):
        f = 100 * FEE(p)
        print(f"   {p:>7.2f}{f:>10.2f}{100*TICK:>8.2f}{f + 100*TICK:>8.2f}")


if __name__ == "__main__":
    main()
