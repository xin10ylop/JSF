"""Fast-cancel maker simulation (mid-window favorite pocket, 15m family).

Question: does canceling within LATENCY seconds of the model fair dropping
below our bid rescue the maker edge that naive resting orders lose to
pick-offs?

Mechanics per signal (grid point, tau in [300,600]s, favorite bid B in
[0.80,0.97], fresh two-sided tape):
- post maker buy at B (size 100, queue 200), max life 60s
- fair path tracked at 100ms from Binance aggTrades x basis ratio, using
  the vol + strike frozen at post time (vol moves slowly over 60s)
- when fair < B + MARGIN, order is canceled LATENCY seconds later
- fills from tape prints at/below B while the order lives
- fills settle at the market result

June 1 - Aug 4 2026 (100ms coverage). Reported per latency.

Usage: python3 src/fastcancel_backtest.py [15m]
"""
import glob
import pickle
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "src")
from fit_gz import predict_gz  # noqa: E402
from ladder_backtest import iter_tapes  # noqa: E402

MARGIN = 0.005
LATENCIES = [0.5, 2.0, 10.0, 1e9]
LIFE_S = 60.0
SIZE = 100.0
QUEUE = 200.0


def main(horizon="15m"):
    fam = f"updown_{horizon}_trades"
    g = pd.read_parquet(f"data/grid_{horizon}.parquet")
    g = g[g.spot_bn.notna() & g.strike.notna()].copy()
    lo_us = int(pd.Timestamp("2026-06-01").value // 1000)
    hi_us = int(pd.Timestamp("2026-08-04").value // 1000)
    g = g[(g.t_us >= lo_us) & (g.t_us < hi_us)]
    g["mid"] = (g.ask_tape + g.bid_tape) / 2
    g["spread"] = g.ask_tape - g.bid_tape
    g = g[(g.trade_age_s < 10) & g.spread.between(0, 0.03)]
    g = g[(g.tau_s >= 300) & (g.tau_s <= 600)]

    ms = pd.concat([pd.read_parquet(f)
                    for f in sorted(glob.glob("data/binance/ms/*.parquet"))],
                   ignore_index=True).sort_values("ds")
    ms_ds = ms.ds.values
    ms_px = ms.price.values.astype("float64")

    cp = pd.read_parquet("data/telonex/chainlink_btcusd.parquet")
    cp_b = cp.sort_values("server_timestamp_us")
    cp_b["bsec"] = cp_b.server_timestamp_us // 1_000_000
    kp = cp_b.groupby("bsec").price_f.last()
    gr = np.arange(kp.index[0], kp.index[-1] + 1)
    kpf = kp.reindex(gr).ffill().values
    bn = pd.concat([pd.read_parquet(f, columns=["open_time", "close"])
                    for f in sorted(glob.glob("data/binance/parquet/*.parquet"))],
                   ignore_index=True)
    bn["sec"] = (bn.open_time // 1_000_000).astype("int64")
    bnp = bn.set_index("sec").close.reindex(gr).ffill().values
    ratio_s = pd.Series(kpf / bnp).rolling(600, min_periods=60).median().shift(1)
    ratio = pd.Series(ratio_s.values, index=gr)

    gz = pickle.load(open("data/gz_models.pkl", "rb"))
    K = gz["K"]

    master = pd.read_parquet("data/master_updown.parquet",
                             columns=["slug", "horizon", "result"])
    master = master[(master.horizon == horizon) & (master.result >= 0)]
    res = dict(zip(master.slug, master.result))

    # signals: one per market per 60s block, favorite side
    up_sig = g[(g.bid_tape >= 0.80) & (g.bid_tape <= 0.97)].copy()
    up_sig["side"] = "Up"
    up_sig["B"] = up_sig.bid_tape
    dn_bid = 1 - g.ask_tape
    dn_sig = g[(dn_bid >= 0.80) & (dn_bid <= 0.97)].copy()
    dn_sig["side"] = "Down"
    dn_sig["B"] = 1 - dn_sig.ask_tape
    sig = pd.concat([up_sig, dn_sig]).sort_values(["slug", "t_us"])
    print(f"signals: {len(sig)} on {sig.slug.nunique()} mkts")

    out = {lat: [] for lat in LATENCIES}
    sig_by_slug = dict(iter(sig.groupby("slug")))
    for slug, tape in iter_tapes(fam):
        ss = sig_by_slug.get(slug)
        if ss is None or slug not in res:
            continue
        result = res[slug]
        tts = tape.timestamp_us.values
        tpx = tape.price.values.astype("float64")
        tsz = tape["size"].values.astype("float64")
        tbuy = tape.is_buy.values
        last_end = 0
        for s in ss.itertuples():
            if s.t_us < last_end:
                continue
            t_post = s.t_us
            t_exp = t_post + int(LIFE_S * 1e6)
            last_end = t_exp
            # fair path at 100ms
            d0 = t_post // 100_000
            d1 = t_exp // 100_000
            i0 = np.searchsorted(ms_ds, d0, "right") - 1
            i1 = np.searchsorted(ms_ds, d1, "right")
            if i0 < 0 or i1 <= i0:
                continue
            seg_ds = ms_ds[i0:i1]
            seg_px = ms_px[i0:i1]
            r = ratio.get(t_post // 1_000_000, np.nan)
            if not np.isfinite(r):
                continue
            spot_path = seg_px * r
            rem_path = (s.t0_us + 900_000_000 - seg_ds * 100_000) / 1e6
            z = (np.log(spot_path / s.strike)
                 / np.sqrt(K * s.ewma5m * np.maximum(rem_path, 1.0)))
            fair = predict_gz(gz, z, np.maximum(rem_path, 1.0))
            if s.side == "Down":
                fair = 1 - fair
            trig = fair < (s.B + MARGIN)
            t_trig = seg_ds[np.argmax(trig)] * 100_000 if trig.any() else None
            if trig.any() and not trig[0]:
                pass
            elif trig.any() and trig[0]:
                t_trig = seg_ds[0] * 100_000
            for lat in LATENCIES:
                t_cxl = min(t_exp, (t_trig + int(lat * 1e6))
                            if t_trig is not None else t_exp)
                j0 = np.searchsorted(tts, t_post, "right")
                j1 = np.searchsorted(tts, t_cxl, "right")
                q = QUEUE
                filled = 0.0
                lvl_up = s.B if s.side == "Up" else 1 - s.B
                for j in range(j0, j1):
                    if s.side == "Up":
                        if tbuy[j]:
                            continue
                        if tpx[j] < lvl_up - 1e-9:
                            take = min(SIZE - filled, tsz[j])
                        elif abs(tpx[j] - lvl_up) <= 1e-9:
                            beyond = max(0.0, tsz[j] - q)
                            q = max(0.0, q - tsz[j])
                            take = min(SIZE - filled, beyond)
                        else:
                            continue
                    else:
                        if not tbuy[j]:
                            continue
                        if tpx[j] > lvl_up + 1e-9:
                            take = min(SIZE - filled, tsz[j])
                        elif abs(tpx[j] - lvl_up) <= 1e-9:
                            beyond = max(0.0, tsz[j] - q)
                            q = max(0.0, q - tsz[j])
                            take = min(SIZE - filled, beyond)
                        else:
                            continue
                    filled += take
                    if filled >= SIZE - 1e-9:
                        break
                if filled > 0:
                    won = (s.side == "Up" and result == 0) or \
                          (s.side == "Down" and result == 1)
                    pnl = filled * ((1.0 if won else 0.0) - s.B)
                    out[lat].append((slug, s.t0_us, filled, s.B, won, pnl))

    for lat in LATENCIES:
        F = pd.DataFrame(out[lat], columns=["slug", "t0_us", "sh", "B",
                                            "won", "pnl"])
        if len(F) == 0:
            print(f"latency {lat}: no fills")
            continue
        tot = F.sh.sum()
        pm = F.groupby("slug").pnl.sum()
        rng = np.random.default_rng(3)
        bs = [np.mean(rng.choice(pm.values, len(pm))) for _ in range(1000)]
        lo, hi = np.quantile(bs, [0.025, 0.975])
        lat_lbl = "inf" if lat > 1e8 else f"{lat}s"
        print(f"latency {lat_lbl:>5}: fills {len(F)} on {F.slug.nunique()} "
              f"mkts, {tot:.0f} sh; alpha {100*F.pnl.sum()/tot:+.2f} c/sh; "
              f"win {np.average(F.won, weights=F.sh):.3f} at "
              f"{np.average(F.B, weights=F.sh):.3f}; "
              f"per-mkt CI [{lo:.3f},{hi:.3f}]")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "15m")
