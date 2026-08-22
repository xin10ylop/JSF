"""Endgame lab: can the live-deployed RollAvg taker be made to fill?

Replays bot/strategy.py::RollAvgEdge second-by-second over recorded
tape, with the bot's EXACT z (bot/state.py::zscore in-window branch) and
its empirical fair (bot/calib.py::p_up), under two fill models:

  LOOKBACK  fill at the last price seen before the decision -- what the
            paper broker effectively assumes, and the model that
            produced the ~$1.3k/day paper line.
  PRINTPROOF fill only where a REAL trade lifted our side's ask at or
            below our limit, AFTER our decision (plus latency). No
            stale quotes, no depth that never traded.

The gap between them is the fill fiction, in cents per share. If
PRINTPROOF stays positive on a gate, that gate is tradeable; if it goes
to zero only under PRINTPROOF, the paper line was never real.

Contract (5m): w = 30s, Up iff mean(P over [t1-w,t1)) >= mean over
[t0-w,t0)); the live gate trades rem in (min_rem_s, min(window_s, w)].
"""
import argparse
import glob
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot.calib import p_up                    # noqa: E402

CACHE = ("/tmp/claude-0/-home-user-JSF/"
         "1e3a1627-7b11-57c3-a9bf-dff6bca1dab2/scratchpad/endgame")


def load(coin=None):
    out = []
    for f in sorted(glob.glob(f"{CACHE}/eg_*.jsonl")):
        if coin and f"eg_{coin}." not in f:
            continue
        for line in open(f):
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            r["path"] = {int(k): v for k, v in r["path"].items()}
            out.append(r)
    return out


def prep(r, w=30.0):
    """Absolute oracle path plus the strike; None if unusable."""
    t0, base = r["t0"], r["base"]
    path = {k + t0: v + base for k, v in r["path"].items()}
    kp = [path[u] for u in range(int(t0 - w), t0) if u in path]
    if len(kp) < w * 0.6:
        return None
    return path, sum(kp) / len(kp)


def z_at(path, K, te, t0, t1, sigma_rel, w=30.0):
    """bot/state.py::zscore. Both branches.

    rem <= w  -- inside the settle window: part of the average is
    already realised, so only `rem` seconds still move it and the
    unrealised part has sd sigma*sqrt(rem^3/3). That variance collapse
    IS the strategy's edge.

    rem > w   -- before the window opens: nothing of the average is
    realised yet, sd is sigma*sqrt(s + w/3) with s = (t1-w) - te. The
    strategy refuses to trade here; this branch exists so the refusal
    can be MEASURED rather than assumed.
    """
    spot = None
    for u in range(te, te - 6, -1):
        if u in path:
            spot = path[u]
            break
    if spot is None:
        return None
    rem = t1 - te
    if rem <= 0:
        return None
    sigma_pre = sigma_rel * spot
    if rem > w:
        s_pre = (t1 - w) - te
        sd_pre = sigma_pre * math.sqrt(max(s_pre, 0.0) + w / 3.0)
        return (spot - K) / sd_pre if sd_pre > 0 else None
    r_sum = 0.0
    n_hole = 0
    for u in range(int(t1 - w), te):
        v = path.get(u)
        if v is None:
            n_hole += 1
            v = spot
        r_sum += v
    if n_hole > 5:
        return None
    sigma = sigma_rel * spot
    sd = sigma * math.sqrt((rem ** 3) / 3.0)
    if sd <= 0:
        return None
    return (r_sum + spot * rem - K * w) / sd


def replay(recs, zmin=2.0, max_price=0.97, min_price=0.0, edge_min=0.02,
           require_edge=False, window_s=60, min_rem_s=2.0, w=30.0,
           lat=0.4, hold=1.5, tick=0.0, fade=False, skip_px=None):
    """Replay the live gate second-by-second under both fill models.

    The live order is a FAK limit: it matches whatever ask is resting
    when it lands and is killed otherwise -- it does NOT wait. So a fill
    requires a real lift of our side's ask, at or below the limit we
    sent, within `hold` seconds of our order landing (te + lat). The
    limit is the price the bot SAW (the prevailing print), because that
    is what it quotes against; paying up later is not something a FAK
    can do.

    Live keeps re-firing on book changes, so every second in the window
    is an attempt: `first signal` scores the paper model, `first fill`
    scores the print-proof one, and their ratio is the fill rate.
    """
    rows = []
    for r in recs:
        pr = prep(r, w)
        if pr is None:
            continue
        path, K = pr
        t0, t1 = r["t0"], r["t1"]
        prints = r["prints"]
        hi = int(window_s)
        sig = None                            # first qualifying signal
        fill = None                           # first FAK that matched
        for te in range(int(t1 - hi), int(t1 - min_rem_s) + 1):
            if fill is not None:
                break
            z = z_at(path, K, te, t0, t1, r["sigma"], w)
            if z is None or abs(z) < zmin:
                continue
            side_up = (z < 0) if fade else (z > 0)
            fair_up = p_up(z)
            fair = fair_up if side_up else 1 - fair_up
            # the price the bot sees: the prevailing print
            back = [p for p in prints if p[0] <= te]
            if not back:
                continue
            up_px = back[-1][1]
            quote = (up_px if side_up else 1 - up_px)
            if not (min_price <= quote <= max_price):
                continue
            if skip_px and skip_px[0] <= quote < skip_px[1]:
                continue
            if require_edge and fair - quote < edge_min:
                continue
            limit = quote + tick              # what we are willing to pay
            won = r["up_won"] if side_up else not r["up_won"]

            def pnl(p):
                return (1.0 if won else 0.0) - p - 0.07 * p * (1 - p)
            if sig is None:
                sig = {"t0": t0, "coin": r["coin"], "z": z, "rem": t1 - te,
                       "won": won, "fair": fair, "px": quote,
                       "pnl_back": pnl(quote)}
            # FAK: does a real lift of our ask at <= limit land in the
            # brief window our order is alive?
            want = "up" if side_up else "down"
            for ts, upx, sz, sd_, oc in prints:
                if ts < te + lat:
                    continue
                if ts > te + lat + hold or ts > t1:
                    break
                if sd_ != "BUY" or oc != want:
                    continue
                p = upx if side_up else 1 - upx
                if p <= limit:
                    fill = {"px_fwd": p, "sz": sz, "rem_fill": t1 - ts,
                            "pnl_fwd": pnl(p)}
                    break
        if sig is None:
            continue
        row = dict(sig)
        row.update(fill or {"px_fwd": None, "pnl_fwd": None, "sz": 0.0})
        rows.append(row)
    return rows


def stats(vals):
    v = [x for x in vals if x is not None]
    n = len(v)
    if n == 0:
        return 0, 0.0, 0.0
    m = sum(v) / n
    var = sum((x - m) ** 2 for x in v) / max(1, n - 1)
    return n, 100 * m, (m / math.sqrt(var / n) if var > 0 else 0.0)


def report(rows, tag):
    nb, evb, tb = stats([r["pnl_back"] for r in rows])
    nf, evf, tf = stats([r["pnl_fwd"] for r in rows])
    days = {}
    for r in rows:
        if r["pnl_fwd"] is not None:
            days.setdefault(time.strftime("%m-%d", time.gmtime(r["t0"])),
                            []).append(r["pnl_fwd"])
    pos = sum(1 for d in days if sum(days[d]) > 0)
    fillrate = 100.0 * nf / nb if nb else 0.0
    apx = [r["px_fwd"] for r in rows if r["px_fwd"] is not None]
    print(f"{tag:<26} lookback n={nb:>5} {evb:>+7.2f}c(t{tb:>+5.1f}) | "
          f"printproof n={nf:>5} {evf:>+7.2f}c(t{tf:>+5.1f}) "
          f"fill{fillrate:>4.0f}% avgpx {sum(apx)/len(apx) if apx else 0:.3f} "
          f"days+ {pos}/{len(days)}")
    return nf, evf, tf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", default=None)
    ap.add_argument("--mode", default="grid")
    a = ap.parse_args()
    recs = load(a.coin)
    coins = sorted({r["coin"] for r in recs})
    print(f"markets: {len(recs)} {[(c, sum(1 for r in recs if r['coin']==c)) for c in coins]}")
    if recs:
        span = (min(r["t0"] for r in recs), max(r["t0"] for r in recs))
        print("span:", time.strftime('%m-%d %H:%M', time.gmtime(span[0])),
              "..", time.strftime('%m-%d %H:%M', time.gmtime(span[1])))
    for c in coins:
        rs = [r for r in recs if r["coin"] == c]
        print(f"\n===== {c} =====")
        report(replay(rs, zmin=2.0, require_edge=False), "LIVE gate z2.0 unfilt")
        report(replay(rs, zmin=2.0, require_edge=True), "LIVE gate z2.0 +edge")
        for zm in (1.0, 1.5, 3.0, 4.0):
            report(replay(rs, zmin=zm, require_edge=False), f"  z{zm} unfilt")
        for lo in (0.70, 0.90):
            report(replay(rs, zmin=2.0, min_price=lo),
                   f"  z2.0 px>={lo}")
        report(replay(rs, zmin=2.0, max_price=0.99), "  z2.0 maxpx0.99")
        report(replay(rs, zmin=2.0, tick=0.01), "  z2.0 +1tick slip")
        report(replay(rs, zmin=2.0, fade=True), "  z2.0 FADE (buy dog)")


if __name__ == "__main__":
    main()
