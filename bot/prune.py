"""Distil recorded books into the endgame evidence, then reclaim the disk.

The recorder writes ~6.5 GB/day of raw CLOB JSONL, which fills a droplet in
days. Almost all of it is irrelevant: the strategy only ever acts inside the
final w seconds of a market, so that is the only book state worth keeping.

For every completed hourly file this extracts, per (market, second), the top
book levels on both tokens inside the last `--window` seconds, writes them to
data/live/books/<hour>.parquet (~3 orders of magnitude smaller), and deletes
the raw file. The parquet is exactly what src/depth_sim.py consumes for the
ex-ante resting-depth test.

Run hourly (systemd timer on the droplet, or `--loop` locally).
"""
import argparse
import glob
import json
import os
import time

import pandas as pd

RAW = "data/live/clob"
OUT = "data/live/books"


def distil(path, window, keep_levels=3):
    rows = []
    with open(path) as fh:
        for line in fh:
            if '"book"' not in line:
                continue
            try:
                e = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if e.get("event_type") != "book":
                continue
            ts_ms = e.get("timestamp")
            if ts_ms is None:
                continue
            ts = int(ts_ms) // 1000
            # market boundaries are unknown here, so keep any snapshot in the
            # tail of BOTH a 5m and a 15m window; both are cheap.
            r5 = 300 - (ts % 300)
            r15 = 900 - (ts % 900)
            if min(r5, r15) > window:
                continue
            # Polymarket orders book levels WORST-to-BEST, so never slice
            # from the front. Sort explicitly to best-first and keep the top
            # levels, rather than depending on the recorder's [-3:] having
            # already done it.
            bids = sorted(((float(x["price"]), float(x["size"]))
                           for x in e.get("bids", [])),
                          key=lambda t: -t[0])[:keep_levels]
            asks = sorted(((float(x["price"]), float(x["size"]))
                           for x in e.get("asks", [])),
                          key=lambda t: t[0])[:keep_levels]
            if not bids and not asks:
                continue
            rows.append({
                "asset_id": str(e.get("asset_id")), "ts": ts,
                "rem5": r5, "rem15": r15,
                "bid_px": [p for p, _ in bids], "bid_sz": [s for _, s in bids],
                "ask_px": [p for p, _ in asks], "ask_sz": [s for _, s in asks],
                "t_local_us": e.get("t_local_us")})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=90,
                    help="seconds before window end to retain")
    ap.add_argument("--min-age-s", type=int, default=3600,
                    help="only touch files this old (i.e. finished hours)")
    ap.add_argument("--keep-raw-hours", type=float, default=2.0)
    ap.add_argument("--loop", type=int, default=0,
                    help="if >0, repeat every N seconds")
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    while True:
        now = time.time()
        freed = 0
        for f in sorted(glob.glob(f"{RAW}/*.jsonl")):
            try:
                age = now - os.path.getmtime(f)
            except OSError:
                continue
            if age < max(a.min_age_s, a.keep_raw_hours * 3600):
                continue
            hour = os.path.basename(f).replace(".jsonl", "")
            outp = f"{OUT}/{hour}.parquet"
            if not os.path.exists(outp):
                try:
                    rows = distil(f, a.window)
                except Exception as e:  # noqa: BLE001
                    print(f"  {hour}: distil failed ({e}); leaving raw")
                    continue
                df = pd.DataFrame(rows) if rows else pd.DataFrame(
                    columns=["asset_id", "ts", "rem5", "rem15", "bid_px",
                             "bid_sz", "ask_px", "ask_sz", "t_local_us"])
                df.to_parquet(outp + ".tmp", index=False)
                os.replace(outp + ".tmp", outp)
                print(f"  {hour}: {len(rows):,} endgame snapshots -> "
                      f"{os.path.getsize(outp)/1e6:.1f} MB")
            sz = os.path.getsize(f)
            os.remove(f)
            freed += sz
            print(f"  {hour}: raw removed ({sz/1e6:.0f} MB)")
        if freed:
            print(f"reclaimed {freed/1e9:.2f} GB")
        if not a.loop:
            break
        time.sleep(a.loop)


if __name__ == "__main__":
    main()
