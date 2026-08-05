"""Fit the empirical symmetrized pricing curve G(z) from oracle data alone.

z = ln(S/K) / sqrt(K_CAL * var_rate * rem).  G maps z -> P(Up wins),
fitted with isotonic regression per remaining-time bucket on train-period
synthetic windows (every 15m/5m boundary window, sampled through its life).
Symmetry G(-z) = 1 - G(z) is imposed by sample augmentation, which kills
trend contamination. Fat tails are captured empirically (t~5 tails; the
final-30s 2-sigma binary is worth ~0.91, not Phi(2)=0.977).

Spot input: basis-adjusted Binance beats last-broadcast oracle spot,
especially in the last 15s (Brier 0.0563 vs 0.0628). Production pricing
uses Binance * rolling median(oracle/Binance).
"""
import glob
import pickle

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression

K_CAL = 1.35
SPLIT = int(pd.Timestamp("2026-06-15").timestamp())
REM_BUCKETS = [(0, 15), (15, 30), (30, 60), (60, 120), (120, 300),
               (300, 600), (600, 900), (900, 1800), (1800, 3600)]


def load_series():
    cp = pd.read_parquet("data/telonex/chainlink_btcusd.parquet")
    cp["sec"] = cp.timestamp_us // 1_000_000
    round_px = cp.sort_values("timestamp_us").groupby("sec").price_f.last()
    grid = np.arange(round_px.index[0], round_px.index[-1] + 1)
    rp = round_px.reindex(grid).ffill().values
    gapmask = (round_px.reindex(grid).isna()
               .rolling(30, min_periods=1).sum().values)
    bn = pd.concat([pd.read_parquet(f, columns=["open_time", "close"])
                    for f in sorted(glob.glob("data/binance/parquet/*.parquet"))],
                   ignore_index=True)
    bn["sec"] = (bn.open_time // 1_000_000).astype("int64")
    bnp = bn.set_index("sec").close.reindex(grid).ffill().values
    cp_b = cp.sort_values("server_timestamp_us")
    cp_b["bsec"] = cp_b.server_timestamp_us // 1_000_000
    kp = cp_b.groupby("bsec").price_f.last().reindex(grid).ffill().values
    ratio = pd.Series(kp / bnp).rolling(600, min_periods=60).median().shift(1).values
    bn_adj = bnp * ratio
    v = pd.read_parquet("data/vol_grid.parquet", columns=["sec", "ewma5m"])
    ev = v.set_index("sec").ewma5m.reindex(grid).ffill().values
    return grid, rp, bn_adj, ev, gapmask


def make_samples(grid, rp, spot, ev, gapmask, win, step, hi_t):
    starts = grid[(grid % win) == 0]
    starts = starts[(starts >= grid[0] + 86400) & (starts + win <= hi_t)]
    i_st = np.searchsorted(grid, starts)
    i_end = np.searchsorted(grid, starts + win).clip(0, len(grid) - 1)
    strike = rp[i_st]
    settle = rp[i_end]
    up = (settle >= strike).astype(float)
    rows = []
    for tau in range(step, win, step):
        t = starts + tau
        i_t = np.searchsorted(grid, t)
        rem = win - tau
        var = ev[i_t - 1]
        ok = ((var > 0) & (gapmask[i_t] < 1) & (gapmask[i_end] < 1)
              & (gapmask[i_st] < 1) & ~np.isnan(spot[i_t]))
        z = np.log(spot[i_t][ok] / strike[ok]) / np.sqrt(K_CAL * var[ok] * rem)
        rows.append(pd.DataFrame({"z": z, "y": up[ok], "rem": rem}))
    return pd.concat(rows, ignore_index=True)


def fit(save="data/gz_models.pkl"):
    grid, rp, bn_adj, ev, gapmask = load_series()
    S = pd.concat([
        make_samples(grid, rp, bn_adj, ev, gapmask, 900, 15, SPLIT),
        make_samples(grid, rp, bn_adj, ev, gapmask, 300, 5, SPLIT),
        make_samples(grid, rp, bn_adj, ev, gapmask, 3600, 60, SPLIT),
    ], ignore_index=True)
    Ssym = pd.concat([S, S.assign(z=-S.z, y=1 - S.y)], ignore_index=True)
    models = {}
    for lo, hi in REM_BUCKETS:
        s = Ssym[(Ssym.rem > lo) & (Ssym.rem <= hi)]
        if len(s) < 8000:
            continue
        iso = IsotonicRegression(y_min=0, y_max=1, out_of_bounds="clip")
        iso.fit(s.z.values, s.y.values)
        models[(lo, hi)] = iso
        print(f"G fit rem ({lo},{hi}]: n={len(s)//2}")
    with open(save, "wb") as f:
        pickle.dump({"K": K_CAL, "models": models,
                     "rem_buckets": REM_BUCKETS}, f)
    print("saved", save)


def predict_gz(models_pkl, z, rem_s):
    """Vectorized G(z) lookup with clipping at |z|<=4."""
    z = np.clip(np.asarray(z, dtype=float), -4, 4)
    rem_s = np.asarray(rem_s)
    out = np.full(len(z), np.nan)
    for (lo, hi), iso in models_pkl["models"].items():
        m = (rem_s > lo) & (rem_s <= hi)
        if m.any():
            out[m] = iso.predict(z[m])
    return out


if __name__ == "__main__":
    fit()
