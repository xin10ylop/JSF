"""Realised P&L per strategy, from the VENUE's ledger -- not our books.

The bot's own day_pnl is an internal accumulator and has been observed
disagreeing with the wallet by ~$9 (it read +0.28 against a real -9).
That number drives the daily stop, so it cannot also be the number we
audit it with. This reads Polymarket's activity feed instead:

    BUY     usdcSize = cash out, fee included
    SELL    usdcSize = cash in,  fee deducted
    REDEEM  usdcSize = cash in

so per market, net = sells + redeems - buys, in real dollars.

A market is only scored once it has SETTLED (t1 plus a grace period for
redemption to land), otherwise an open position reads as a total loss.
A market with a SELL was exited early -- the jump scalp; one held to
REDEEM or to nothing is the endgame taker.

    python3 src/pnl.py                 # today
    python3 src/pnl.py --hours 6 --by-market
"""
import argparse
import json
import os
import time
import urllib.request
from collections import defaultdict

API = "https://data-api.polymarket.com/activity"
GRACE_S = 180.0          # redemption lands a minute or two after t1


def fetch(wallet, hours):
    cutoff = time.time() - hours * 3600
    rows, offset = [], 0
    while offset < 2000:
        url = f"{API}?user={wallet}&limit=100&offset={offset}"
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0"})
            arr = json.loads(urllib.request.urlopen(req, timeout=25).read())
        except Exception as e:                          # noqa: BLE001
            print(f"fetch failed at offset {offset}: {e}")
            break
        if not arr:
            break
        rows.extend(arr)
        if min(int(x.get("timestamp") or 0) for x in arr) < cutoff:
            break
        offset += 100
        time.sleep(0.05)
    return [x for x in rows if int(x.get("timestamp") or 0) >= cutoff]


def t1_of(slug):
    """Settlement time from the slug: <coin>-updown-<fam>-<t0>."""
    try:
        parts = slug.split("-")
        t0 = int(parts[-1])
        fam = parts[-2]
        step = {"5m": 300, "15m": 900, "1h": 3600}.get(fam)
        return t0 + step if step else None
    except Exception:                                   # noqa: BLE001
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wallet", default=os.environ.get("POLYMARKET_FUNDER"))
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--by-market", action="store_true")
    a = ap.parse_args()
    if not a.wallet:
        print("no wallet: pass --wallet 0x... or set POLYMARKET_FUNDER")
        return
    rows = fetch(a.wallet, a.hours)
    if not rows:
        print("no activity in window")
        return

    mk = defaultdict(lambda: {"buy": 0.0, "sell": 0.0, "redeem": 0.0,
                              "sh": 0.0, "px": [], "t": 0})
    for x in rows:
        slug = x.get("slug") or "?"
        usd = float(x.get("usdcSize") or 0)
        d = mk[slug]
        d["t"] = max(d["t"], int(x.get("timestamp") or 0))
        if x.get("type") == "REDEEM":
            d["redeem"] += usd
        elif str(x.get("side")).upper() == "BUY":
            d["buy"] += usd
            d["sh"] += float(x.get("size") or 0)
            d["px"].append(float(x.get("price") or 0))
        elif str(x.get("side")).upper() == "SELL":
            d["sell"] += usd

    now = time.time()
    groups = {"SCALP (sold early)": [], "ENDGAME (held to settle)": []}
    open_n = 0
    for slug, d in mk.items():
        t1 = t1_of(slug)
        if t1 is None or now < t1 + GRACE_S:
            open_n += 1
            continue                      # unsettled: not a loss yet
        if d["buy"] <= 0:
            continue
        d["slug"], d["net"] = slug, d["sell"] + d["redeem"] - d["buy"]
        groups["SCALP (sold early)" if d["sell"] > 0
               else "ENDGAME (held to settle)"].append(d)

    tot = 0.0
    for name, v in groups.items():
        if not v:
            continue
        v.sort(key=lambda d: d["t"])
        net = sum(d["net"] for d in v)
        sh = sum(d["sh"] for d in v)
        wins = sum(1 for d in v if d["net"] > 0)
        tot += net
        print(f"\n{name}")
        if a.by_market:
            for d in v:
                ap_ = sum(d["px"]) / len(d["px"]) if d["px"] else 0
                print(f"   {time.strftime('%H:%M', time.localtime(d['t']))}"
                      f"  {d['slug'][-10:]}  in {d['buy']:6.2f}"
                      f"  out {d['sell'] + d['redeem']:6.2f}"
                      f"  net {d['net']:+7.2f}   {d['sh']:5.1f} sh"
                      f" @ {ap_:.3f}")
        print(f"   {len(v)} markets, {wins} up / {len(v)-wins} down"
              f"   net ${net:+.2f}"
              f"   {100*net/sh if sh else 0:+.2f} c/share on {sh:.0f} sh")
        worst = min(v, key=lambda d: d["net"])
        best = max(v, key=lambda d: d["net"])
        print(f"   best {best['net']:+.2f}   worst {worst['net']:+.2f}"
              f"   ({worst['slug'][-10:]})")

    print(f"\nTOTAL realised, last {a.hours:g}h: ${tot:+.2f}"
          f"   ({open_n} market(s) still open, not counted)")
    print("Source: Polymarket activity feed -- the venue's own cash "
          "record, independent of the bot's day_pnl.")


if __name__ == "__main__":
    main()
