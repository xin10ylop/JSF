"""Decision grid for the hourly up-or-down family (Binance-settled).

Strike = 1H candle open (h_open in master, verified 100%). Spot and vol are
pure Binance (no oracle). Grid every 20s, dense 2s in the last 120s.
Output: data/grid_hourly.parquet with an fv column from G(z) directly.
"""
import glob
import pickle
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "src")
from fit_gz import predict_gz  # noqa: E402

WIN = 3600


def main():
    master = pd.read_parquet("data/master_hourly.parquet")
    master = master[(master.result >= 0) & master.h_open.notna()]
    trades = pd.read_parquet("data/consolidated/hourly_updown_trades.parquet")
    trades = trades.sort_values("timestamp_us")
    have = set(trades.slug.unique())
    master = master[master.slug.isin(have)].reset_index(drop=True)
    print(f"hourly: {len(master)} resolved markets with tape+strike")

    bn = pd.concat([pd.read_parquet(f, columns=["open_time", "close"])
                    for f in sorted(glob.glob("data/binance/parquet/*.parquet"))],
                   ignore_index=True)
    bn["sec"] = (bn.open_time // 1_000_000).astype("int64")
    bn = bn.sort_values("sec").drop_duplicates("sec")
    bn_sec = bn.sec.values
    bn_close = bn.close.values
    vol = pd.read_parquet("data/vol_grid.parquet", columns=["sec", "ewma5m"])
    vol_sec = vol.sec.values
    vol_v = vol.ewma5m.values
    gz = pickle.load(open("data/gz_models.pkl", "rb"))
    K = gz["K"]

    rows = []
    tr_by_slug = dict(iter(trades.groupby("slug")))
    for _, m in master.iterrows():
        tape = tr_by_slug.get(m.slug)
        if tape is None or len(tape) == 0:
            continue
        tts = tape.timestamp_us.values
        tpx = tape.price.values.astype("float64")
        tbuy = tape.is_buy.values
        coarse = np.arange(20, WIN - 120, 20, dtype="int64")
        dense = np.arange(WIN - 120, WIN, 2, dtype="int64")
        off = np.unique(np.concatenate([coarse, dense]))
        gt = m.t0_us + off * 1_000_000

        idx = np.searchsorted(tts, gt, side="right") - 1
        ok = idx >= 0
        last_px = np.where(ok, tpx[idx.clip(0)], np.nan)
        age = np.where(ok, (gt - tts[idx.clip(0)]) / 1e6, np.nan)
        bmask = tbuy
        smask = ~tbuy
        ia = np.searchsorted(tts[bmask], gt, side="right") - 1
        ib = np.searchsorted(tts[smask], gt, side="right") - 1
        ask = np.where(ia >= 0, tpx[bmask][ia.clip(0)], np.nan)
        bid = np.where(ib >= 0, tpx[smask][ib.clip(0)], np.nan)

        bsec = (gt // 1_000_000) - 1
        bi = np.searchsorted(bn_sec, bsec, side="right") - 1
        spot = np.where(bi >= 0, bn_close[bi.clip(0)], np.nan)
        vi = np.searchsorted(vol_sec, bsec, side="right") - 1
        var = vol_v[vi.clip(0)]
        rem = (WIN - off).astype("float64")
        z = np.log(spot / m.h_open) / np.sqrt(K * var * rem)
        fv = predict_gz(gz, z, rem)

        n = len(gt)
        rows.append(pd.DataFrame({
            "slug": np.repeat(m.slug, n), "t_us": gt, "tau_s": off,
            "rem_s": rem, "t0_us": np.repeat(m.t0_us, n),
            "result": np.repeat(m.result, n),
            "strike": np.repeat(m.h_open, n),
            "last_px": last_px, "ask_tape": ask, "bid_tape": bid,
            "trade_age_s": age, "spot_bn": spot, "ewma5m": var,
            "z": z, "fv": fv,
        }))
    grid = pd.concat(rows, ignore_index=True)
    grid.to_parquet("data/grid_hourly.parquet", compression="zstd")
    print(f"grid_hourly: {len(grid)} rows, {grid.slug.nunique()} markets")


if __name__ == "__main__":
    main()
