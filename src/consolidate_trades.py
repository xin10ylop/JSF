"""Consolidate per-market trade parquets into per-family tables.

Slims columns, writes one parquet per family, and (with --purge) deletes the
raw per-market files afterwards to keep disk usage bounded.
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


def consolidate(family: str, raw_dir: str, purge: bool = False,
                out_dir: str = "data/consolidated"):
    os.makedirs(out_dir, exist_ok=True)
    out = f"{out_dir}/{family}.parquet"
    files = sorted(glob.glob(f"{raw_dir}/*.parquet"))
    if not files:
        print(f"{family}: no files yet")
        return
    frames = []
    bad = []
    for f in files:
        try:
            df = pd.read_parquet(f, columns=KEEP)
        except Exception:
            bad.append(f)
            continue
        if len(df):
            frames.append(df)
    if not frames:
        print(f"{family}: nothing readable ({len(bad)} bad)")
        return
    allf = pd.concat(frames, ignore_index=True)
    allf["price"] = allf.price.astype("float32")
    allf["size"] = allf["size"].astype("float32")
    allf["is_buy"] = (allf.side == "buy")
    allf["mirrored"] = allf.origin_asset_id != allf.asset_id
    allf = allf.drop(columns=["side", "origin_asset_id", "asset_id"])
    allf = allf.sort_values(["slug", "timestamp_us"]).reset_index(drop=True)
    if os.path.exists(out):
        prev = pd.read_parquet(out)
        allf = (pd.concat([prev, allf], ignore_index=True)
                .drop_duplicates(["slug", "timestamp_us", "price", "size", "is_buy"])
                .sort_values(["slug", "timestamp_us"]).reset_index(drop=True))
    allf.to_parquet(out, compression="zstd")
    print(f"{family}: {len(files)} files ({len(bad)} bad) -> {len(allf)} trades")
    if purge:
        for f in files:
            if f not in bad:
                os.remove(f)
        print(f"{family}: purged {len(files)-len(bad)} raw files")


if __name__ == "__main__":
    purge = "--purge" in sys.argv
    which = [a for a in sys.argv[1:] if not a.startswith("--")] or list(FAMILIES)
    for fam in which:
        consolidate(fam, FAMILIES[fam], purge=purge)
