"""Open-jump study: is the price move right after open predictable?

Tests the structure "decide at t-10, buy near 0.50 at open, exit on a
small favourable move" -- independent of WHO makes the prediction.

The point is to bound the opportunity before anyone builds a predictor:

  ceiling   perfect foresight of the t+X price. No model can beat it,
            so if this is small after fees the idea is dead regardless
            of how good the forecaster is.
  signals   simple candidates an LLM would also be looking at (BTC
            momentum over several lookbacks, and the open price's own
            skew). If none of these beat a coin flip, a language model
            reading the same candles has no obvious advantage.
  exits     sell-limit at a target vs holding to settlement, measured
            on the same trades.

Prints must be PAGINATED: the trades API returns newest-first with a
1000-row cap, so on busy markets the opening prints -- exactly what
this study needs -- are missing without paging. That truncation is
what made an earlier at-open study read +14c/sh when the honest
number was ~0.

    python3 src/openjump_fetch.py --coin btc --day-from 3 --day-to 0 \\
        --out oj_btc.jsonl
"""
import argparse
import json
import math
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

GAMMA = "https://gamma-api.polymarket.com/markets"
BINANCE = "https://data-api.binance.vision/api/v3/klines"
TRADES = "https://data-api.polymarket.com/trades"


def get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0"})
            return json.loads(urllib.request.urlopen(req, timeout=25).read())
        except Exception:  # noqa: BLE001
            time.sleep(0.6 + i)
    return None


def all_prints(cond, t_from):
    """Page back until coverage reaches t_from (newest-first API)."""
    rows = []
    for off in range(0, 12000, 1000):
        arr = get(f"{TRADES}?market={cond}&limit=1000&offset={off}")
        if not arr:
            break
        rows.extend(arr)
        oldest = min((int(x.get("timestamp") or 0) for x in arr), default=0)
        if oldest > 10 ** 12:
            oldest //= 1000
        if len(arr) < 1000 or oldest < t_from:
            break
        time.sleep(0.05)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", default="btc")
    ap.add_argument("--sym", default="BTCUSDT")
    ap.add_argument("--day-from", type=float, required=True)
    ap.add_argument("--day-to", type=float, required=True)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    step = 300
    now = int(time.time())
    t_hi = now - int(a.day_to * 86400)
    t_lo = now - int(a.day_from * 86400)

    out_lock = threading.Lock()
    state = {"ok": 0}
    fh = open(a.out, "w")

    def one(t0):
        t1 = t0 + step
        mk = get(f"{GAMMA}?slug={a.coin}-updown-5m-{t0}&closed=true")
        if not mk:
            return
        mk = mk[0]
        toks = mk.get("clobTokenIds")
        toks = json.loads(toks) if isinstance(toks, str) else toks
        op = mk.get("outcomePrices")
        op = json.loads(op) if isinstance(op, str) else op
        if not op or not toks:
            return
        v = float(op[0])
        if 0.01 < v < 0.99:
            return
        # 1s BTC path spanning the decision point and the exit horizons
        ks = get(f"{BINANCE}?symbol={a.sym}&interval=1s"
                 f"&startTime={(t0 - 130) * 1000}"
                 f"&endTime={(t0 + 95) * 1000}&limit=1000")
        if not ks:
            return
        path = {int(r[0]) // 1000: float(r[4]) for r in ks}
        if len([u for u in path if t0 - 60 <= u <= t0 + 60]) < 90:
            return
        prints = []
        for x in all_prints(mk.get("conditionId"), t0 - 60):
            ts = int(x.get("timestamp") or 0)
            if ts > 10 ** 12:
                ts //= 1000
            if not (t0 - 60 <= ts <= t0 + 95):
                continue
            p = float(x.get("price", 0))
            sz = float(x.get("size", 0))
            if not (0 < p < 1):
                continue
            oc = str(x.get("outcome") or "").lower()
            asset = str(x.get("asset") or "")
            up = p if (asset == str(toks[0]) or oc == "up") else 1 - p
            prints.append([ts, round(up, 4), round(sz, 2),
                           str(x.get("side") or "")])
        if len(prints) < 4:
            return
        base = min(path.values())
        rec = {"t0": t0, "coin": a.coin, "up_won": v > 0.5,
               "base": round(base, 4),
               "path": {str(u - t0): round(p - base, 4)
                        for u, p in sorted(path.items())},
               "prints": sorted(prints)}
        with out_lock:
            fh.write(json.dumps(rec) + "\n")
            state["ok"] += 1
            if state["ok"] % 100 == 0:
                fh.flush()
                print(f"[{a.coin}] {state['ok']} cached", flush=True)

    grid = list(range((t_lo // step) * step + step, t_hi - 2 * step, step))
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        list(ex.map(one, grid))
    fh.close()
    print(f"[{a.coin}] DONE {state['ok']}/{len(grid)} -> {a.out}", flush=True)


if __name__ == "__main__":
    main()
