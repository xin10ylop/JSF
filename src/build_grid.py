"""Build the decision-grid dataset for updown families.

For each resolved market with oracle + trades coverage, sample the window on
a fixed grid. Every feature uses only information available at grid time t:
tape state (last price, tape-implied bid/ask, flow), oracle spot by broadcast
time, Binance spot, causal vol estimators. Output: data/grid_<horizon>.parquet
"""
import sys

import numpy as np
import pandas as pd

GRID_STEP = {"5m": 5, "15m": 10, "4h": 60}
WINDOW_S = {"5m": 300, "15m": 900, "4h": 14400}


def last_leq(sorted_ts, values, query_ts, max_age_us=None):
    idx = np.searchsorted(sorted_ts, query_ts, side="right") - 1
    ok = idx >= 0
    out = np.full(len(query_ts), np.nan)
    out[ok] = values[idx[ok]]
    if max_age_us is not None:
        age = query_ts - np.where(ok, sorted_ts[idx.clip(0)], 0)
        out[~ok | (age > max_age_us)] = np.nan
    return out, idx


def build(family: str):
    horizon = family.replace("updown_", "").replace("_trades", "")
    step = GRID_STEP[horizon]
    win = WINDOW_S[horizon]

    master = pd.read_parquet("data/master_updown.parquet")
    master = master[(master.horizon == horizon) & (master.result >= 0)
                    & master.strike.notna() & master.settle_px.notna()]
    trades = pd.read_parquet(f"data/consolidated/{family}.parquet")
    trades = trades.sort_values("timestamp_us")
    have = set(trades.slug.unique())
    master = master[master.slug.isin(have)].sort_values("t0_us").reset_index(drop=True)
    print(f"{family}: {len(master)} resolved markets with tape+oracle")

    cp = pd.read_parquet("data/telonex/chainlink_btcusd.parquet",
                         columns=["timestamp_us", "server_timestamp_us", "price_f"])
    cp = cp.sort_values("server_timestamp_us")
    cl_bts = cp.server_timestamp_us.values
    cl_val = cp.price_f.values

    vol = pd.read_parquet("data/vol_grid.parquet")
    vol_sec = vol.sec.values
    vol_cols = [c for c in vol.columns if c != "sec"]
    vol_vals = {c: vol[c].values for c in vol_cols}

    bn_files = sorted(__import__("glob").glob("data/binance/parquet/*.parquet"))
    bn = pd.concat([pd.read_parquet(f, columns=["open_time", "close"])
                    for f in bn_files], ignore_index=True)
    bn["sec"] = (bn.open_time // 1_000_000).astype("int64")
    bn = bn.sort_values("sec").drop_duplicates("sec")
    bn_sec = bn.sec.values
    bn_close = bn.close.values

    rows = []
    tr_by_slug = dict(iter(trades.groupby("slug")))
    for _, m in master.iterrows():
        tape = tr_by_slug.get(m.slug)
        if tape is None or len(tape) == 0:
            continue
        tts = tape.timestamp_us.values
        tpx = tape.price.values.astype("float64")
        tsz = tape["size"].values.astype("float64")
        tbuy = tape.is_buy.values
        bmask = tbuy
        smask = ~tbuy

        coarse = np.arange(step, max(win - 60, step), step, dtype="int64")
        dense = np.arange(max(win - 60, step), win, 2, dtype="int64")
        grid_off = np.unique(np.concatenate([coarse, dense]))
        gt = m.t0_us + grid_off * 1_000_000

        last_px, li = last_leq(tts, tpx, gt)
        ask_px, _ = last_leq(tts[bmask], tpx[bmask], gt)
        bid_px, _ = last_leq(tts[smask], tpx[smask], gt)
        c_signed = np.concatenate([[0.0], np.cumsum(np.where(tbuy, tsz, -tsz))])
        c_tot = np.concatenate([[0.0], np.cumsum(tsz)])
        i_now = np.searchsorted(tts, gt, side="right")
        i_60 = np.searchsorted(tts, gt - 60_000_000, side="right")
        flow60 = c_signed[i_now] - c_signed[i_60]
        vol60 = c_tot[i_now] - c_tot[i_60]
        age = np.where(li >= 0, (gt - tts[li.clip(0)]) / 1e6, np.nan)

        spot, _ = last_leq(cl_bts, cl_val, gt, max_age_us=30_000_000)
        bsec = (gt // 1_000_000) - 1
        bi = np.searchsorted(bn_sec, bsec, side="right") - 1
        bn_spot = np.where(bi >= 0, bn_close[bi.clip(0)], np.nan)
        vi = np.searchsorted(vol_sec, bsec, side="right") - 1
        vfeat = {c: vol_vals[c][vi.clip(0)] for c in vol_cols}

        n = len(gt)
        rec = {
            "slug": np.repeat(m.slug, n),
            "t_us": gt,
            "tau_s": grid_off,
            "rem_s": win - grid_off,
            "t0_us": np.repeat(m.t0_us, n),
            "result": np.repeat(m.result, n),
            "strike": np.repeat(m.strike, n),
            "settle_px": np.repeat(m.settle_px, n),
            "last_px": last_px,
            "ask_tape": ask_px,
            "bid_tape": bid_px,
            "trade_age_s": age,
            "flow60": flow60,
            "vol60": vol60,
            "spot_cl": spot,
            "spot_bn": bn_spot,
        }
        rec.update(vfeat)
        rows.append(pd.DataFrame(rec))

    grid = pd.concat(rows, ignore_index=True)
    grid.to_parquet(f"data/grid_{horizon}.parquet", compression="zstd")
    print(f"grid_{horizon}: {len(grid)} rows, {grid.slug.nunique()} markets")


if __name__ == "__main__":
    for fam in sys.argv[1:] or ["updown_15m_trades"]:
        build(fam)
