"""Resumable bulk downloader for Telonex Polymarket data.

Downloads per-market parquet files listed in a manifest CSV. Handles 403
quota exhaustion gracefully. Manifest columns: channel,date,slug,outcome,
asset_id,out_name (extra columns ignored).
"""
import argparse
import csv
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE = "https://api.telonex.io/v1/downloads/polymarket"


def download_one(session, key, row, out_dir):
    out = out_dir / row["out_name"]
    if out.exists() and out.stat().st_size > 200:
        return ("skip", row["out_name"], 0)
    params = {}
    if row.get("slug"):
        params["slug"] = row["slug"]
    if row.get("outcome"):
        params["outcome"] = row["outcome"]
    if row.get("asset_id"):
        params["asset_id"] = row["asset_id"]
    url = f"{BASE}/{row['channel']}/{row['date']}"
    for attempt in range(4):
        try:
            r = session.get(url, headers={"Authorization": f"Bearer {key}"},
                            params=params, timeout=120)
            if r.status_code == 200:
                tmp = out.with_suffix(".tmp")
                tmp.write_bytes(r.content)
                tmp.rename(out)
                return ("ok", row["out_name"], len(r.content))
            if r.status_code == 403:
                return ("quota", row["out_name"], r.text[:200])
            if r.status_code == 404:
                return ("missing", row["out_name"], 0)
            time.sleep(2 ** attempt)
        except Exception as e:  # noqa: BLE001
            if attempt == 3:
                return ("error", row["out_name"], str(e)[:200])
            time.sleep(2 ** attempt)
    return ("error", row["out_name"], "retries exhausted")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    key = os.environ["TELONEX_API_KEY"]
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = list(csv.DictReader(open(args.manifest)))
    todo = [r for r in rows if not (out_dir / r["out_name"]).exists()]
    if args.limit:
        todo = todo[: args.limit]
    print(f"manifest {len(rows)} rows, {len(todo)} to download", flush=True)

    stats = {"ok": 0, "skip": 0, "missing": 0, "error": 0, "quota": 0}
    nbytes = 0
    t0 = time.time()
    quota_hit = False
    with requests.Session() as s, ThreadPoolExecutor(args.concurrency) as ex:
        futs = {ex.submit(download_one, s, key, r, out_dir): r for r in todo}
        for i, f in enumerate(as_completed(futs)):
            status, name, info = f.result()
            stats[status] += 1
            if status == "ok":
                nbytes += info
            elif status == "quota":
                if not quota_hit:
                    print(f"QUOTA HIT: {info}", flush=True)
                    quota_hit = True
                for other in futs:
                    other.cancel()
                break
            elif status == "error":
                print(f"ERR {name}: {info}", flush=True)
            if (i + 1) % 1000 == 0:
                el = time.time() - t0
                print(f"{i+1}/{len(todo)} ok={stats['ok']} miss={stats['missing']} "
                      f"err={stats['error']} {nbytes/1e6:.0f}MB {el:.0f}s", flush=True)
    print(f"DONE {stats} bytes={nbytes/1e6:.1f}MB in {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
