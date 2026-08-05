"""Decision grid from real book_snapshot_5 data for the sampled markets."""
import glob
import os
import re

import numpy as np
import pandas as pd

GRID_STEP = {"5m": 5, "15m": 10, "4h": 60, "hourly": 20, "above": 20}
WINDOW_S = {"5m": 300, "15m": 900, "4h": 14400, "hourly": 3600, "above": 3600}

LEVELS = 5
COLS = ["timestamp_us"] + [f"{s}_{w}_{i}" for s in ("bid", "ask")
                           for w in ("price", "size") for i in range(LEVELS)]


def fam_of(slug):
    if re.match(r"btc-updown-5m-", slug):
        return "5m"
    if re.match(r"btc-updown-15m-", slug):
        return "15m"
    if re.match(r"btc-updown-4h-", slug):
        return "4h"
    if re.match(r"bitcoin-up-or-down-", slug):
        return "hourly"
    if re.match(r"bitcoin-above-", slug):
        return "above"
    return None


def load_masters():
    m = {}
    ud = pd.read_parquet("data/master_updown.parquet")
    for _, r in ud.iterrows():
        m[r.slug] = (r.t0_us, r.t1_us, r.result)
    hr = pd.read_parquet("data/master_hourly.parquet")
    for _, r in hr.iterrows():
        m[r.slug] = (r.t0_us, r.t1_us, r.result)
    ab = pd.read_parquet("data/master_above.parquet")
    for _, r in ab.iterrows():
        m[r.slug] = (r.t1_us - 3600_000_000, r.t1_us, r.result)
    return m


def build():
    masters = load_masters()
    files = sorted(glob.glob("data/telonex/raw/book5_sample/*.parquet"))
    by_slug = {}
    for f in files:
        slug = os.path.basename(f).split("__")[0]
        by_slug.setdefault(slug, []).append(f)
    print(f"book files: {len(files)}, markets: {len(by_slug)}")

    out_rows = {k: [] for k in GRID_STEP}
    for slug, fl in by_slug.items():
        fam = fam_of(slug)
        if fam is None or slug not in masters:
            continue
        t0, t1, result = masters[slug]
        if result < 0 or t0 <= 0:
            continue
        try:
            df = pd.concat([pd.read_parquet(f, columns=COLS) for f in fl],
                           ignore_index=True)
        except Exception:
            continue
        if len(df) == 0:
            continue
        df = df.sort_values("timestamp_us")
        ts = df.timestamp_us.values
        step = GRID_STEP[fam]
        win = WINDOW_S[fam]
        gt = t0 + np.arange(step, win, step, dtype="int64") * 1_000_000
        idx = np.searchsorted(ts, gt, side="right") - 1
        ok = idx >= 0
        if ok.sum() == 0:
            continue
        rec = {"slug": np.repeat(slug, ok.sum()), "t_us": gt[ok],
               "tau_s": (gt[ok] - t0) // 1_000_000,
               "rem_s": (t1 - gt[ok]) // 1_000_000,
               "result": np.repeat(result, ok.sum()),
               "book_age_s": (gt[ok] - ts[idx[ok]]) / 1e6}
        sub = df.iloc[idx[ok]]
        for c in COLS[1:]:
            rec[c] = pd.to_numeric(sub[c], errors="coerce").values
        out_rows[fam].append(pd.DataFrame(rec))

    for fam, rows in out_rows.items():
        if not rows:
            continue
        grid = pd.concat(rows, ignore_index=True)
        grid.to_parquet(f"data/book_grid_{fam}.parquet", compression="zstd")
        print(f"book_grid_{fam}: {len(grid)} rows, {grid.slug.nunique()} markets")


if __name__ == "__main__":
    build()
