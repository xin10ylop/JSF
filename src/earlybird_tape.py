"""EarlyBird tape study: buy EARLY (rem 90-240s) where books are thick,
exit by RESTING a sell at 0.97/0.99 into the endgame taker demand.

The structure came from live evidence: the favourite's ask side empties
in the final ~30-60s (measured: 171 taker kills vs 14 fills), while
mid-window books at 0.5-0.85 are two-sided and liquid, and our own
kills prove starved BUY demand exists at 0.97-0.99 for a resting sell
to feed. A quick 69-market study of the raw structure (buy whatever the
market favours) measured ~0 EV -- as theory demands: paying the
market's own price is paying fair. The edge must come from the MODEL
picking the side: enter only when the pre-window z says the market's
price is wrong by >= edge_min.

This script measures exactly that on recorded data (droplet):
  oracle series  data/live/rtds/*.jsonl   (recorder, 1s Chainlink ticks)
  market prices  clob prices-history      (1m fidelity)
  outcomes       gamma (venue resolution, authoritative)

Pre-window z (reconcile_bot's offline formula): with s = rem - w,
    z = (spot - K) / (sigma_rel * spot * sqrt(s + w/3))
fair = p_up(z), the same empirical calibration the bot trades.

Conservative by construction: entry pays price + one tick + taker fee;
the sell counts as filled only if a 1-MINUTE price point crosses the
level (misses intraminute touches); maker exit pays no fee (venue:
fees are taker-only on this series).

Run on the droplet:
    python3 src/earlybird_tape.py --coin btc --hours 24
"""
import argparse
import glob
import json
import math
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot.calib import p_up            # noqa: E402
from bot.state import COINS           # noqa: E402

GAMMA = "https://gamma-api.polymarket.com/markets"
CLOB = "https://clob.polymarket.com/prices-history"


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def load_oracle(symbol, since_s):
    px = {}
    for f in sorted(glob.glob("data/live/rtds/*.jsonl")):
        try:
            with open(f) as fh:
                for line in fh:
                    if symbol not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:  # noqa: BLE001
                        continue
                    p = d.get("payload", {})
                    if p.get("symbol") != symbol:
                        continue
                    ts = int(p["timestamp"]) // 1000
                    if ts >= since_s:
                        px[ts] = float(p["value"])
        except OSError:
            continue
    return px


def held(px, t, max_back=8):
    """Last oracle value at or before t (held price), or None."""
    for u in range(t, t - max_back, -1):
        if u in px:
            return px[u]
    return None


def sigma_rel(px, t, lookback=3600):
    """1s log-return sd over [t-lookback, t), gap-tolerant."""
    prev = None
    acc = []
    for u in range(t - lookback, t):
        v = px.get(u)
        if v is None:
            continue
        if prev is not None and prev[1] > 0:
            dt = u - prev[0]
            if 0 < dt <= 10:
                acc.append(math.log(v / prev[1]) / math.sqrt(dt))
        prev = (u, v)
    if len(acc) < 300:
        return None
    mu = sum(acc) / len(acc)
    var = sum((a - mu) ** 2 for a in acc) / len(acc)
    return math.sqrt(var)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", default="btc")
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--fam", default="5m", choices=["5m", "15m"])
    ap.add_argument("--w", type=float, default=60.0)
    ap.add_argument("--edge-min", type=float, default=0.03)
    ap.add_argument("--max-entry", type=float, default=0.85)
    ap.add_argument("--min-entry", type=float, default=0.15)
    a = ap.parse_args()
    step = 300 if a.fam == "5m" else 900
    sym = COINS[a.coin]["oracle"]
    now = int(time.time())
    since = now - int(a.hours * 3600) - 7200
    print(f"loading oracle series {sym} since {a.hours + 2:.0f}h ago ...")
    px = load_oracle(sym, since)
    print(f"  {len(px)} oracle seconds loaded")
    if len(px) < 3600:
        print("not enough recorder data (data/live/rtds); aborting")
        return

    t_latest = now // step * step - 2 * step
    markets = []
    for k in range(int(a.hours * 3600 / step)):
        t0 = t_latest - step * k
        slug = f"{a.coin}-updown-{a.fam}-{t0}"
        try:
            arr = get(f"{GAMMA}?slug={slug}&closed=true")
            if not arr:
                continue
            mk = arr[0]
            toks = mk.get("clobTokenIds")
            toks = json.loads(toks) if isinstance(toks, str) else toks
            op = mk.get("outcomePrices")
            op = json.loads(op) if isinstance(op, str) else op
            v = float(op[0])
            if 0.01 < v < 0.99:
                continue                      # voided
            h = get(f"{CLOB}?market={toks[0]}&startTs={t0 - 60}"
                    f"&endTs={t0 + step}&fidelity=1")
            pts = [(int(x["t"]), float(x["p"]))
                   for x in h.get("history", [])]
            if len(pts) < 3:
                continue
            markets.append((t0, v > 0.5, pts))
        except Exception:  # noqa: BLE001
            continue
        time.sleep(0.12)
    print(f"  {len(markets)} settled {a.fam} markets with price history\n")

    def z_at(t0, te):
        t1 = t0 + step
        K_pts = [px[u] for u in range(int(t0 - a.w), int(t0)) if u in px]
        if len(K_pts) < a.w / 3:
            return None, None
        K = sum(K_pts) / len(K_pts)
        spot = held(px, te)
        sr = sigma_rel(px, te)
        if spot is None or sr is None or sr <= 0:
            return None, None
        s = (t1 - a.w) - te
        if s < 0:
            return None, None                 # in-window: not this study
        z = (spot - K) / (sr * spot * math.sqrt(s + a.w / 3))
        return z, float(p_up(z))

    header = (f"{'rem':>4} {'zmin':>4} {'sell@':>5} {'n':>4} "
              f"{'EV_c/sh':>8} {'sold%':>6} {'win%':>6} {'avg_in':>7} "
              f"{'$/day@15sh':>10}")
    print(header)
    day_frac = 24.0 / a.hours
    for rem in (240, 180, 150, 120, 90):
        for zmin in (1.5, 2.0, 2.5):
            for sell_at in (0.97, 0.99):
                n = wins = sells = 0
                ev_sum = cost_sum = 0.0
                for t0, up_won, pts in markets:
                    te = t0 + step - rem
                    z, fair = z_at(t0, te)
                    if z is None or abs(z) < zmin:
                        continue
                    side_up = z > 0
                    fair_side = fair if side_up else 1 - fair
                    cand = [p for (t, p) in pts if t <= te]
                    if not cand:
                        continue
                    mkt_up = cand[-1]
                    price = (mkt_up if side_up else 1 - mkt_up) + 0.01
                    if not (a.min_entry <= price <= a.max_entry):
                        continue
                    if fair_side - price < a.edge_min:
                        continue              # model must disagree in
                    n += 1                    # our favour, or no trade
                    fee = 0.07 * price * (1 - price)
                    later = [(t, p) for (t, p) in pts if t > te]
                    if side_up:
                        touched = any(p >= sell_at for _, p in later)
                        won = up_won
                    else:
                        touched = any(p <= 1 - sell_at for _, p in later)
                        won = not up_won
                    pay = sell_at if touched else (1.0 if won else 0.0)
                    ev_sum += pay - price - fee
                    cost_sum += price
                    wins += won
                    sells += touched
                if n:
                    print(f"{rem:>4} {zmin:>4} {sell_at:>5} {n:>4} "
                          f"{100 * ev_sum / n:>+8.2f} "
                          f"{100 * sells / n:>5.0f}% "
                          f"{100 * wins / n:>5.0f}% "
                          f"{cost_sum / n:>7.3f} "
                          f"{15 * ev_sum / n * n * day_frac:>+10.2f}")
    print("\nReading: EV_c/sh net of entry fee+tick; sold% = exited to the "
          "endgame takers at sell@ (maker, no fee); the rest held to the "
          "venue's settlement. Positive EV at n>=30 across neighbouring "
          "cells = the EarlyBird edge is real; single hot cells are noise.")


if __name__ == "__main__":
    main()
