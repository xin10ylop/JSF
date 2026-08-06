"""Consolidate per-market trade parquets into per-family tables.

Chunked and resumable: processes raw files in batches, writes part files,
then merges parts into one family parquet. With --purge, deletes raw files
after their part is safely written.
"""
import glob
import os
import sys

import pandas as pd

FAMILIES = {
    "updown_15m_trades": "data/telonex/raw/updown_15m_trades",
    "updown_5m_trades": "data/telonex/raw/updown_5m_trades",
    "updown_4h_trades": "data/telonex/raw/updown_4h_trades",
    "hourly_updown_trades": "data/telonex/raw/hourly_updown_trades",
    "above_hourly_trades": "data/telonex/raw/above_hourly_trades",
}

KEEP = ["timestamp_us", "local_timestamp_us", "slug", "price", "size", "side",
        "origin_asset_id", "asset_id"]
CHUNK = 4000


def consolidate(family: str, raw_dir: str, purge: bool = False,
                out_dir: str = "data/consolidated"):
    os.makedirs(out_dir, exist_ok=True)
    part_dir = f"{out_dir}/parts_{family}"
    os.makedirs(part_dir, exist_ok=True)
    out = f"{out_dir}/{family}.parquet"
    files = sorted(glob.glob(f"{raw_dir}/*.parquet"))
    if files:
        for ci in range(0, len(files), CHUNK):
            chunk = files[ci:ci + CHUNK]
            part = f"{part_dir}/part_{ci//CHUNK:04d}.parquet"
            if not os.path.exists(part):
                frames = []
                bad = []
                for f in chunk:
                    try:
                        df = pd.read_parquet(f, columns=KEEP)
                    except Exception:
                        bad.append(f)
                        continue
                    if len(df):
                        frames.append(df)
                if frames:
                    allf = pd.concat(frames, ignore_index=True)
                    allf["price"] = allf.price.astype("float32")
                    allf["size"] = allf["size"].astype("float32")
                    allf["is_buy"] = (allf.side == "buy")
                    allf["mirrored"] = allf.origin_asset_id != allf.asset_id
                    allf = allf.drop(columns=["side", "origin_asset_id",
                                              "asset_id"])
                    tmp = part + ".tmp"
                    allf.to_parquet(tmp, compression="zstd")
                    os.replace(tmp, part)
                print(f"{family}: part {ci//CHUNK} "
                      f"({len(chunk)} files, {len(bad)} bad)", flush=True)
            if purge:
                for f in chunk:
                    try:
                        os.remove(f)
                    except OSError:
                        pass
    parts = sorted(glob.glob(f"{part_dir}/part_*.parquet"))
    if not parts:
        print(f"{family}: no parts")
        return
    allf = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    allf = allf.sort_values(["slug", "timestamp_us"]).reset_index(drop=True)
    tmp = out + ".tmp"
    allf.to_parquet(tmp, compression="zstd")
    os.replace(tmp, out)
    print(f"{family}: consolidated {len(allf)} trades -> {out}", flush=True)
    for p in parts:
        os.remove(p)
    os.rmdir(part_dir)


if __name__ == "__main__":
    purge = "--purge" in sys.argv
    which = [a for a in sys.argv[1:] if not a.startswith("--")] or list(FAMILIES)
    for fam in which:
        consolidate(fam, FAMILIES[fam], purge=purge)
