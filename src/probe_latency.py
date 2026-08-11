"""Measure the real round trip to Polymarket from the host that trades.

`latency_ms` in the config was a guess, and it was smaller than the venue's
own mandatory hold. This replaces the guess with a measurement.

Total time from "we decide" to "we could match" is

    rtt/2  (our request reaching the matching engine)
  + 250 ms (the venue's documented hold on crypto up/down markets)
  + rtt/2  (the result coming back -- irrelevant to the fill, but it is
            what bounds how fast we can react to our own state)

We cannot time an authenticated order without keys, so this times the
signed-path proxy that shares the same host, TLS termination and region: a
POST to the CLOB that is rejected on auth, plus a GET of a real order book.
Both traverse the full network path; only the matching work differs.

Run it ON the trading host:

    python3 src/probe_latency.py --n 40
"""
import argparse
import json
import statistics
import time

import requests

CLOB = "https://clob.polymarket.com"
UA = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}

# One session for every sample. A fresh TLS handshake per request measures
# the handshake, not the trading path -- the live bot holds its connection
# open, so warm round trips are the number that belongs in the config.
S = requests.Session()
S.headers.update(UA)


def timed(method, url, body=None, timeout=10):
    t0 = time.perf_counter()
    try:
        r = S.request(method, url, data=body, timeout=timeout)
        r.content
    except Exception:  # noqa: BLE001
        return None, None
    return (time.perf_counter() - t0) * 1000.0, r.status_code


def report(name, xs):
    if not xs:
        print(f"  {name:<22} no successful samples")
        return None
    xs = sorted(xs)
    p50 = statistics.median(xs)
    p90 = xs[int(0.9 * (len(xs) - 1))]
    print(f"  {name:<22} n={len(xs):<3} p50 {p50:7.1f} ms   "
          f"p90 {p90:7.1f} ms   min {xs[0]:7.1f}   max {xs[-1]:7.1f}")
    return p50


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--hold-ms", type=float, default=250.0,
                    help="venue taker hold on crypto up/down markets")
    ap.add_argument("--write", action="store_true",
                    help="write the measured p50 into bot/config.json")
    a = ap.parse_args()

    # a real, cheap GET that hits the CLOB service itself
    warm, _ = timed("GET", f"{CLOB}/time")
    print(f"warm-up {warm:.0f} ms (TLS handshake included)"
          if warm else "warm-up failed")

    gets, posts = [], []
    for _ in range(a.n):
        ms, _c = timed("GET", f"{CLOB}/time")
        if ms is not None:
            gets.append(ms)
        # unsigned POST: rejected at auth, but only after the full round
        # trip through the same edge and service the real order would take
        ms, _c = timed("POST", f"{CLOB}/order", json.dumps({"probe": 1}))
        if ms is not None:
            posts.append(ms)
        time.sleep(0.05)

    print("\n=== round trip to clob.polymarket.com ===")
    g = report("GET /time", gets)
    p = report("POST /order (unauth)", posts)
    # The unauth POST short-circuits at the auth check, so it under-states
    # a real order; GET /time reaches the service. Take the larger.
    cands = [x for x in (g, p) if x is not None]
    if not cands:
        print("\nno samples; cannot set rtt_ms")
        return
    rtt = max(cands)
    print(f"\nSet in bot/config.json:  \"rtt_ms\": {round(rtt)}")
    print(f"Total taker delay to model: {round(rtt)} + {a.hold_ms:.0f} "
          f"(venue hold) = {round(rtt + a.hold_ms)} ms")
    if a.write:
        # bot/config.local.json, NOT the tracked config: rtt_ms is a
        # property of this machine, and writing it into a tracked file made
        # `git pull` abort on local changes -- which is how a deploy
        # silently did nothing while looking like it had worked.
        import os
        p = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "bot", "config.local.json")
        cfg = {}
        if os.path.exists(p):
            with open(p) as fh:
                cfg = json.load(fh)
        old = cfg.get("rtt_ms")
        cfg["rtt_ms"] = round(rtt)
        with open(p, "w") as fh:
            json.dump(cfg, fh, indent=2)
            fh.write("\n")
        print(f"wrote rtt_ms {old} -> {round(rtt)} into {p} "
              f"(gitignored; restart the bot to pick it up)")
    print("\nThe matching engine is in AWS eu-west-2 (London). If p50 is "
          "well above ~20 ms you are paying for distance: a London or "
          "Dublin host cuts it to single digits, and the measured edge "
          "decays about 0.5c/share per second of decision lag.")


if __name__ == "__main__":
    main()
