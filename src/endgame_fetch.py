"""Endgame lab fetcher: everything needed to replay the RollAvg endgame
taker (the live-deployed strategy) against real prints, offline.

Per market it stores:
  outcome   gamma resolution (authoritative)
  oracle    1s closes over [t0-70, t1+5]  -> the bot's exact z at ANY
            moment of the settle window
  prints    real executed trades in the final `tail` seconds, with size
            and side -> print-proof fills (we only fill where someone
            actually traded at our price, AFTER our decision)

Note on pagination: the trades API returns NEWEST-first with a 1000-row
cap. For the at-open study that truncation was fatal (opening prints
lost on busy markets) and had to be paged around; for the ENDGAME the
newest rows ARE the window we want, so page 1 suffices.

Oracle proxy: binance 1s closes stand in for the Chainlink TWAP (the
recorder's rtds series lives on the droplet). Same proxy the original
paper backtest used, so results are comparable to it.

Threaded: the fetch is pure network latency (3 calls/market), so a
small pool turns hours into minutes. Order is not preserved; the lab
sorts on load.
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", required=True)
    ap.add_argument("--sym", required=True)
    ap.add_argument("--fam", default="5m")
    ap.add_argument("--day-from", type=float, required=True)
    ap.add_argument("--day-to", type=float, required=True)
    ap.add_argument("--tail", type=int, default=200,
                    help="seconds before t1 to keep prints for")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    step = 300 if a.fam == "5m" else 900
    now = int(time.time())
    t_hi = now - int(a.day_to * 86400)
    t_lo = now - int(a.day_from * 86400)

    closes = {}
    t = t_lo - 7200
    while t < t_hi:
        k = get(f"{BINANCE}?symbol={a.sym}&interval=1m"
                f"&startTime={t * 1000}&limit=1000")
        if not k:
            break
        for r in k:
            closes[int(r[0]) // 1000] = float(r[4])
        t = int(k[-1][0]) // 1000 + 60
        time.sleep(0.05)
    print(f"[{a.coin}] 1m candles: {len(closes)}", flush=True)

    sig_cache, sig_lock = {}, threading.Lock()

    def sigma1s(ts):
        key = ts // 1800
        with sig_lock:
            if key in sig_cache:
                return sig_cache[key]
        xs = sorted(u for u in closes if ts - 3600 <= u < ts)
        rs = [math.log(closes[b] / closes[c])
              for c, b in zip(xs, xs[1:]) if 50 <= b - c <= 70]
        v = None
        if len(rs) >= 30:
            mu = sum(rs) / len(rs)
            v = math.sqrt(sum((r - mu) ** 2 for r in rs) / len(rs) / 60.0)
        with sig_lock:
            sig_cache[key] = v
        return v

    out_lock = threading.Lock()
    state = {"ok": 0}
    fh = open(a.out, "w")

    def one(t0):
        t1 = t0 + step
        slug = f"{a.coin}-updown-{a.fam}-{t0}"
        mk = get(f"{GAMMA}?slug={slug}&closed=true")
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
            return                            # voided / unresolved
        ks = get(f"{BINANCE}?symbol={a.sym}&interval=1s"
                 f"&startTime={(t0 - 70) * 1000}"
                 f"&endTime={(t1 + 5) * 1000}&limit=1000")
        if not ks:
            return
        path = {int(r[0]) // 1000: float(r[4]) for r in ks}
        if len([u for u in path if t0 - 30 <= u < t0]) < 20:
            return                            # strike window unusable
        if len([u for u in path if t1 - 30 <= u < t1]) < 20:
            return                            # settle window unusable
        sr = sigma1s(t0)
        if not sr:
            return
        arr = get(f"{TRADES}?market={mk.get('conditionId')}&limit=1000")
        prints = []
        for x in arr or []:
            ts = int(x.get("timestamp") or 0)
            if ts > 10 ** 12:
                ts //= 1000
            if not (t1 - a.tail <= ts <= t1 + 5):
                continue
            p = float(x.get("price", 0))
            sz = float(x.get("size", 0))
            if not (0 < p < 1):
                continue
            oc = str(x.get("outcome") or "").lower()
            asset = str(x.get("asset") or "")
            up_px = p if (asset == str(toks[0]) or oc == "up") else 1 - p
            prints.append([ts, round(up_px, 4), round(sz, 2),
                           str(x.get("side") or ""), oc])
        if len(prints) < 3:
            return
        base = min(path.values())
        rec = {"t0": t0, "t1": t1, "coin": a.coin, "up_won": v > 0.5,
               "sigma": round(sr, 10), "base": round(base, 4),
               "path": {str(u - t0): round(p - base, 4)
                        for u, p in sorted(path.items())},
               "prints": sorted(prints)}
        with out_lock:
            fh.write(json.dumps(rec) + "\n")
            state["ok"] += 1
            if state["ok"] % 200 == 0:
                fh.flush()
                print(f"[{a.coin}] {state['ok']} cached", flush=True)

    grid = list(range((t_lo // step) * step + step, t_hi - 2 * step, step))
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        list(ex.map(one, grid))
    fh.close()
    print(f"[{a.coin}] DONE {state['ok']}/{len(grid)} -> {a.out}", flush=True)


if __name__ == "__main__":
    main()
