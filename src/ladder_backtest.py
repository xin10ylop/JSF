"""Resurrection-ladder backtest.

At tau = POST_FRAC of the window, post maker bids at fixed deep levels on
BOTH sides (Up and Down). Orders rest until window end. Fills via the
conservative print rule (prints strictly below level always fill; prints at
level fill beyond queue). Positions redeem at settlement.

The bet: conditional on a side collapsing through deep levels late, the
crowd oversells - the fill price is below the true resurrection probability
(fat tails + panic). Fee-free (maker).

Usage: POST_FRAC=0.9 QUEUE=200 python3 src/ladder_backtest.py 15m [test]
Env: LEVELS="0.05,0.10,0.15,0.20,0.25"  SIZE=100
"""
import os
import sys

import numpy as np
import pandas as pd

SPLIT_US = int(pd.Timestamp("2026-06-15").value // 1000)
WIN_S = {"5m": 300, "15m": 900, "4h": 14400, "hourly": 3600}


def run(horizon, use_test=False):
    fam = ("hourly_updown_trades" if horizon == "hourly"
           else f"updown_{horizon}_trades")
    win = WIN_S[horizon]
    post_frac = float(os.environ.get("POST_FRAC", 0.9))
    queue = float(os.environ.get("QUEUE", 200.0))
    size = float(os.environ.get("SIZE", 100.0))
    levels = [float(x) for x in
              os.environ.get("LEVELS", "0.05,0.10,0.15,0.20,0.25").split(",")]

    if horizon == "hourly":
        m = pd.read_parquet("data/master_hourly.parquet",
                            columns=["slug", "t0_us", "result"])
        m = m[m.result >= 0]
    else:
        m = pd.read_parquet("data/master_updown.parquet",
                            columns=["slug", "horizon", "t0_us", "result"])
        m = m[(m.horizon == horizon) & (m.result >= 0)]
    m = m[m.t0_us >= SPLIT_US] if use_test else m[m.t0_us < SPLIT_US]
    res = dict(zip(m.slug, m.result))
    t0s = dict(zip(m.slug, m.t0_us))

    # optional model gate: only ladder the side whose model fair >= FV_GATE
    # at posting time (fv from the decision grid, nearest point <= t_post)
    fv_gate = float(os.environ.get("FV_GATE", 0.0))
    fv_map = {}
    if fv_gate > 0 and horizon == "hourly":
        g = pd.read_parquet("data/grid_hourly.parquet",
                            columns=["slug", "tau_s", "fv"])
        g = g[g.fv.notna()]
        post_off_s = float(os.environ.get("POST_FRAC", 0.9)) * WIN_S[horizon]
        gg = g[g.tau_s <= post_off_s].sort_values("tau_s").groupby("slug").last()
        fv_map = dict(zip(gg.index, gg.fv))
    elif fv_gate > 0:
        import pickle
        sys.path.insert(0, "src")
        from fit_gz import predict_gz
        import glob as _glob
        g = pd.read_parquet(f"data/grid_{horizon}.parquet",
                            columns=["slug", "t_us", "tau_s", "rem_s",
                                     "spot_bn", "strike", "ewma5m"])
        g = g[g.spot_bn.notna() & g.strike.notna()]
        cp = pd.read_parquet("data/telonex/chainlink_btcusd.parquet")
        cp_b = cp.sort_values("server_timestamp_us")
        cp_b["bsec"] = cp_b.server_timestamp_us // 1_000_000
        kp = cp_b.groupby("bsec").price_f.last()
        gr = np.arange(kp.index[0], kp.index[-1] + 1)
        kpf = kp.reindex(gr).ffill().values
        bn = pd.concat([pd.read_parquet(f, columns=["open_time", "close"])
                        for f in sorted(_glob.glob("data/binance/parquet/*.parquet"))],
                       ignore_index=True)
        bn["sec"] = (bn.open_time // 1_000_000).astype("int64")
        bnp = bn.set_index("sec").close.reindex(gr).ffill().values
        ratio = pd.Series(kpf / bnp).rolling(600, min_periods=60).median().shift(1)
        g["spot_adj"] = g.spot_bn * pd.Series(ratio.values, index=gr).reindex(
            g.t_us // 1_000_000).values
        gz = pickle.load(open("data/gz_models.pkl", "rb"))
        g["z"] = (np.log(g.spot_adj / g.strike)
                  / np.sqrt(gz["K"] * g.ewma5m * g.rem_s))
        g["fv"] = predict_gz(gz, g.z.values, g.rem_s.values)
        g = g[g.fv.notna()]
        # fv at the last grid point <= post time for each slug
        post_off_s = float(os.environ.get("POST_FRAC", 0.9)) * WIN_S[horizon]
        gg = g[g.tau_s <= post_off_s].sort_values("tau_s").groupby("slug").last()
        fv_map = dict(zip(gg.index, gg.fv))

    t = pd.read_parquet(f"data/consolidated/{fam}.parquet",
                        columns=["timestamp_us", "slug", "price", "size",
                                 "is_buy"])
    t = t[t.slug.isin(res)]
    t = t.sort_values(["slug", "timestamp_us"])

    post_off = int(post_frac * win * 1e6)
    fills = []
    for slug, tape in t.groupby("slug"):
        result = res[slug]
        t0 = t0s[slug]
        t_post = t0 + post_off
        t_end = t0 + win * 1_000_000
        tts = tape.timestamp_us.values
        i0 = np.searchsorted(tts, t_post, side="right")
        i1 = np.searchsorted(tts, t_end, side="right")
        if i1 <= i0:
            continue
        tpx = tape.price.values[i0:i1].astype("float64")
        tsz = tape["size"].values[i0:i1].astype("float64")
        tbuy = tape.is_buy.values[i0:i1]
        # Up-side ladder: sell prints (is_buy False) at price <= level
        # Down-side ladder: buy prints at price >= 1 - level
        sides = ("Up", "Down")
        if fv_gate > 0:
            fv = fv_map.get(slug)
            if fv is None:
                continue
            if fv >= fv_gate:
                sides = ("Up",)
            elif fv <= 1 - fv_gate:
                sides = ("Down",)
            else:
                continue
        for lvl in levels:
            for side in sides:
                q = queue
                rem = size
                got = 0.0
                if side == "Up":
                    mask_thr = ~tbuy
                    for p, s, ok in zip(tpx, tsz, mask_thr):
                        if not ok or rem <= 0:
                            continue
                        if p < lvl - 1e-9:
                            take = min(rem, s)
                        elif abs(p - lvl) <= 1e-9:
                            beyond = max(0.0, s - q)
                            q = max(0.0, q - s)
                            take = min(rem, beyond)
                        else:
                            continue
                        rem -= take
                        got += take
                else:
                    lvl_up = 1 - lvl
                    for p, s, ok in zip(tpx, tsz, tbuy):
                        if not ok or rem <= 0:
                            continue
                        if p > lvl_up + 1e-9:
                            take = min(rem, s)
                        elif abs(p - lvl_up) <= 1e-9:
                            beyond = max(0.0, s - q)
                            q = max(0.0, q - s)
                            take = min(rem, beyond)
                        else:
                            continue
                        rem -= take
                        got += take
                if got > 0:
                    won = (side == "Up" and result == 0) or \
                          (side == "Down" and result == 1)
                    pnl = got * ((1.0 if won else 0.0) - lvl)
                    fills.append((slug, t0, side, lvl, got, won, pnl))

    if not fills:
        print("NO FILLS")
        return
    F = pd.DataFrame(fills, columns=["slug", "t0_us", "side", "level",
                                     "shares", "won", "pnl"])
    F["day"] = pd.to_datetime(F.t0_us, unit="us").dt.date
    stake = (F.shares * F.level).sum()
    print(f"POST_FRAC={post_frac} QUEUE={queue} SIZE={size} LEVELS={levels} "
          f"{'TEST' if use_test else 'TRAIN'}")
    print(f"fills: {len(F)} on {F.slug.nunique()} mkts, {F.day.nunique()} days; "
          f"{F.shares.sum():.0f} sh, ${stake:.0f} stake")
    print(f"PnL ${F.pnl.sum():.2f}; {100*F.pnl.sum()/F.shares.sum():.2f} c/sh; "
          f"return on stake {100*F.pnl.sum()/stake:.1f}%")
    by_lvl = F.groupby("level").agg(n=("pnl", "size"), sh=("shares", "sum"),
                                    win=("won", "mean"), pnl=("pnl", "sum"))
    by_lvl["alpha_c"] = 100 * by_lvl.pnl / by_lvl.sh
    print(by_lvl.round(3).to_string())
    # day-cluster bootstrap on total pnl per day
    by_day = F.groupby("day").pnl.sum()
    rng = np.random.default_rng(17)
    days = by_day.values
    bs = [np.mean(rng.choice(days, len(days))) for _ in range(2000)]
    lo, hi = np.quantile(bs, [0.025, 0.975])
    print(f"per-day PnL mean ${by_day.mean():.2f} day-cluster CI "
          f"[${lo:.2f}, ${hi:.2f}] over {len(by_day)} days; "
          f"negative days {(by_day<0).mean():.2f}")
    top = by_day.sort_values().tail(5)
    print(f"top-5 days: {top.sum()/by_day.sum():.2f} of total; "
          f"best day ${by_day.max():.0f}, worst ${by_day.min():.0f}")


if __name__ == "__main__":
    run(sys.argv[1], use_test="test" in sys.argv)
