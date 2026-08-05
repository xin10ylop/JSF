"""Causal volatility estimators on the Binance 1s grid.

Per-second variance rates from 1s log returns using only past data, plus a
causal hour-of-week seasonality factor. Output: data/vol_grid.parquet
"""
import glob

import numpy as np
import pandas as pd

WINDOWS = {"rv5m": 300, "rv15m": 900, "rv1h": 3600, "rv4h": 14400, "rv24h": 86400}
EWMA_HALFLIVES = {"ewma1m": 60, "ewma5m": 300, "ewma30m": 1800}


def build():
    files = sorted(glob.glob("data/binance/parquet/*.parquet"))
    bn = pd.concat(
        [pd.read_parquet(f, columns=["open_time", "close"]) for f in files],
        ignore_index=True,
    )
    bn["sec"] = (bn.open_time // 1_000_000).astype("int64")
    bn = bn.sort_values("sec").drop_duplicates("sec").reset_index(drop=True)
    sec = bn.sec.values
    grid = np.arange(sec[0], sec[-1] + 1)
    close = pd.Series(bn.close.values, index=sec).reindex(grid).ffill().values
    r = np.diff(np.log(close), prepend=np.log(close[0]))
    r[0] = 0.0
    r2 = r * r
    absr = np.abs(r)

    out = pd.DataFrame({"sec": grid})
    c2 = np.concatenate([[0.0], np.cumsum(r2)])
    for name, w in WINDOWS.items():
        s = c2[w:] - c2[:-w]
        rate = np.full(len(grid), np.nan)
        rate[w - 1:] = s / w
        out[name] = rate.astype("float32")

    bp = absr[1:] * absr[:-1]
    cbp = np.concatenate([[0.0, 0.0], np.cumsum(bp)])
    w = 900
    bpv = np.full(len(grid), np.nan)
    bpv[w:] = (np.pi / 2.0) * (cbp[w + 1:] - cbp[1:-w]) / w
    out["bpv15m"] = bpv.astype("float32")

    for name, hl in EWMA_HALFLIVES.items():
        alpha = 1 - np.exp(np.log(0.5) / hl)
        out[name] = (
            pd.Series(r2).ewm(alpha=alpha, adjust=False).mean().values.astype("float32")
        )

    dt = pd.to_datetime(out.sec, unit="s")
    df = pd.DataFrame({"rv5m": out.rv5m.values}, index=dt)
    hourly = df.rv5m.resample("1h").mean()
    hh = pd.DataFrame({"rv": hourly})
    hh["how"] = hh.index.dayofweek * 24 + hh.index.hour
    hh["seasonal"] = np.nan
    for h in range(168):
        m = hh.how == h
        hh.loc[m, "seasonal"] = (
            hh.loc[m, "rv"].shift(1).rolling(4, min_periods=2).mean()
        )
    hh["overall"] = hh.rv.shift(1).rolling(168 * 4, min_periods=100).mean()
    hh["factor"] = (hh.seasonal / hh.overall).clip(0.2, 5.0)
    fac = hh.factor.reindex(dt.dt.floor("1h")).values
    out["season_factor"] = pd.Series(fac).fillna(1.0).values.astype("float32")

    out.to_parquet("data/vol_grid.parquet", compression="zstd")
    print("vol grid rows:", len(out), "span:",
          pd.to_datetime(grid[0], unit="s"), "->", pd.to_datetime(grid[-1], unit="s"))


if __name__ == "__main__":
    build()
