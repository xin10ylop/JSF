"""Fetch 1-second Binance klines via the public REST API.

data.binance.vision only publishes a day's archive well after that day
closes, so recent days (and live operation) need the REST endpoint. Output
is written in the same zip-of-csv shape the archive uses, so every consumer
of data/binance_alts keeps working unchanged.

Public endpoint, no key, no quota beyond IP rate limits.
"""
import argparse
import io
import os
import time
import zipfile
from datetime import datetime, timezone

import requests

BASE = "https://data-api.binance.vision/api/v3/klines"
SYM = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT",
       "xrp": "XRPUSDT", "doge": "DOGEUSDT"}


def day_bounds(d):
    t = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(t.timestamp()) * 1000, (int(t.timestamp()) + 86400) * 1000


def fetch_day(sym, day, s):
    start, end = day_bounds(day)
    rows, cur = [], start
    while cur < end:
        for attempt in range(5):
            r = s.get(BASE, params={"symbol": sym, "interval": "1s",
                                    "startTime": cur, "endTime": end,
                                    "limit": 1000}, timeout=30)
            if r.status_code == 200:
                break
            time.sleep(1.5 * (attempt + 1))
        else:
            return None
        js = r.json()
        if not js:
            break
        rows += js
        nxt = js[-1][0] + 1000
        if nxt <= cur:
            break
        cur = nxt
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", nargs="+", default=list(SYM))
    ap.add_argument("--days", nargs="+", required=True)
    ap.add_argument("--out", default="data/binance_alts")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    os.makedirs(a.out, exist_ok=True)
    for coin in a.coins:
        sym = SYM[coin]
        for day in a.days:
            fp = f"{a.out}/{sym}-1s-{day}.zip"
            if os.path.exists(fp) and not a.force:
                print(f"skip {sym} {day}")
                continue
            rows = fetch_day(sym, day, s)
            if not rows:
                print(f"MISS {sym} {day}")
                continue
            buf = io.StringIO()
            for r in rows:
                buf.write(",".join(str(x) for x in r) + "\n")
            with zipfile.ZipFile(fp + ".tmp", "w",
                                 zipfile.ZIP_DEFLATED) as z:
                z.writestr(f"{sym}-1s-{day}.csv", buf.getvalue())
            os.replace(fp + ".tmp", fp)
            print(f"ok {sym} {day}: {len(rows):,} bars")


if __name__ == "__main__":
    main()
