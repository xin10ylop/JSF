"""Historical simulation of the GzValueMaker strategy on the tape.

At each grid time t (fresh two-sided tape quotes): fair = G(z) with
basis-adjusted Binance spot. If fair(side) - bid(side) >= edge_min, post a
maker buy at bid(side) with TTL, size = SIZE. Fills simulated with the
conservative rule (prints strictly through the level fill; prints at the
level fill beyond queue_ahead, which is taken from the book-sample-derived
typical top size for the phase). Positions held to settlement. Maker pays
no fee; rebates not credited.

Outputs per-market P&L, fill stats, capacity, cluster CIs, monthly slices,
and null benchmarks. Train period only unless 'test' passed.

Usage: python3 src/strategy_backtest.py 15m [edge_min] [ttl_s] [test]
"""
import glob
import pickle
import sys

import numpy as np
import pandas as pd

from fit_gz import predict_gz

SPLIT_US = int(pd.Timestamp("2026-06-15").value // 1000)
SIZE = 100.0
QUEUE_TYPICAL = float(__import__("os").environ.get("QUEUE", 400.0))


def basis_ratio_series():
    cp = pd.read_parquet("data/telonex/chainlink_btcusd.parquet")
    cp_b = cp.sort_values("server_timestamp_us")
    cp_b["bsec"] = cp_b.server_timestamp_us // 1_000_000
    kp = cp_b.groupby("bsec").price_f.last()
    grid = np.arange(kp.index[0], kp.index[-1] + 1)
    kpf = kp.reindex(grid).ffill().values
    bn = pd.concat([pd.read_parquet(f, columns=["open_time", "close"])
                    for f in sorted(glob.glob("data/binance/parquet/*.parquet"))],
                   ignore_index=True)
    bn["sec"] = (bn.open_time // 1_000_000).astype("int64")
    bnp = bn.set_index("sec").close.reindex(grid).ffill().values
    ratio = pd.Series(kpf / bnp).rolling(600, min_periods=60).median().shift(1)
    return pd.Series(ratio.values, index=grid)


def run(horizon, edge_min=0.03, ttl_s=20.0, use_test=False):
    fam = f"updown_{horizon}_trades"
    g = pd.read_parquet(f"data/grid_{horizon}.parquet")
    g = g[g.spot_bn.notna() & g.strike.notna() & (g.rem_s > 0)].copy()
    g["train"] = g.t0_us < SPLIT_US
    g = g[~g.train] if use_test else g[g.train]
    g["mid"] = (g.ask_tape + g.bid_tape) / 2
    g["spread"] = g.ask_tape - g.bid_tape
    g = g[(g.trade_age_s < 10) & g.spread.between(0.0, 0.03)]

    ratio = basis_ratio_series()
    g["spot_adj"] = g.spot_bn * ratio.reindex(g.t_us // 1_000_000).values
    with open("data/gz_models.pkl", "rb") as f:
        gz = pickle.load(f)
    K = gz["K"]
    g["z"] = (np.log(g.spot_adj / g.strike)
              / np.sqrt(K * g.ewma5m * g.rem_s))
    g["fv"] = predict_gz(gz, g.z.values, g.rem_s.values)
    g = g[g.fv.notna()]

    mode = "flb" if "flb" in sys.argv else "gz"
    if mode == "gz":
        # buy Up at bid when fv - bid >= edge_min; Down mirrored
        up_sig = g[(g.fv - g.bid_tape) >= edge_min].copy()
        up_sig["side"] = "Up"
        up_sig["level"] = up_sig.bid_tape
        dn_sig = g[(g.ask_tape - g.fv) >= edge_min].copy()
        dn_sig["side"] = "Down"
        dn_sig["level"] = 1 - dn_sig.ask_tape
    else:
        # FLB-primary pockets, parameterized via env:
        #   POCKET=early_fav | endgame_long
        import os
        win_s = {"15m": 900, "5m": 300, "4h": 14400}[horizon]
        pocket = os.environ.get("POCKET", "early_fav")
        if pocket == "early_fav":
            ph = g[g.tau_s <= 0.33 * win_s]
            lo, hi = 0.70, 0.97
        elif pocket == "endgame_offer":
            # sell the leader above model fair in the last seconds:
            # economically a sibling buy at 1-L; fills when chasers lift L.
            margin = float(os.environ.get("MARGIN", 0.05))
            ph = g[(g.tau_s >= float(os.environ.get("EG_FRAC", 0.985))
                    * win_s)].copy()
            # Up is leader: offer Up at L >= fv+margin (>= current ask)
            up_lead = ph[(ph.fv >= 0.5)].copy()
            L_up = np.maximum(up_lead.fv + margin,
                              up_lead.ask_tape).clip(upper=0.99)
            up_lead["side"] = "Down"        # we END UP long Down at 1-L
            up_lead["level"] = 1 - L_up     # sibling-equivalent entry
            # Down is leader: offer Down at Ld >= (1-fv)+margin
            dn_lead = ph[(ph.fv < 0.5)].copy()
            L_dn = np.maximum((1 - dn_lead.fv) + margin,
                              1 - dn_lead.bid_tape).clip(upper=0.99)
            dn_lead["side"] = "Up"          # long Up at 1-L_dn
            dn_lead["level"] = 1 - L_dn
            sig = pd.concat([up_lead, dn_lead], ignore_index=True)
            sig = sig[(sig.level >= 0.01) & (sig.level <= 0.45)]
            up_sig = sig[sig.side == "Up"]
            dn_sig = sig[sig.side == "Down"]
        else:  # endgame_long: bid for abandoned longshots, last 1.5%
            ph = g[g.tau_s >= 0.985 * win_s]
            lo, hi = 0.03, 0.30
        if pocket != "endgame_offer":
            up_sig = ph[(ph.bid_tape >= lo) & (ph.bid_tape <= hi)].copy()
            up_sig["side"] = "Up"
            up_sig["level"] = up_sig.bid_tape
            dn_bid = 1 - ph.ask_tape
            dn_sig = ph[(dn_bid >= lo) & (dn_bid <= hi)].copy()
            dn_sig["side"] = "Down"
            dn_sig["level"] = 1 - dn_sig.ask_tape
    sig = pd.concat([up_sig, dn_sig], ignore_index=True)
    sig = sig.sort_values(["slug", "t_us"])
    print(f"signals: {len(sig)} at {sig.slug.nunique()} markets "
          f"(edge_min={edge_min}, ttl={ttl_s}s)")

    trades = pd.read_parquet(f"data/consolidated/{fam}.parquet")
    trades = trades.sort_values(["slug", "timestamp_us"])
    tr_by_slug = dict(iter(trades.groupby("slug")))

    fills = []
    for slug, ss in sig.groupby("slug"):
        tape = tr_by_slug.get(slug)
        if tape is None:
            continue
        tts = tape.timestamp_us.values
        tpx = tape.price.values.astype("float64")
        tsz = tape["size"].values.astype("float64")
        tbuy = tape.is_buy.values
        result = int(ss.result.iloc[0])
        last_end = 0
        for _, s in ss.iterrows():
            if s.t_us < last_end:      # one live order per market at a time
                continue
            t_post = s.t_us
            t_cxl = t_post + int(ttl_s * 1e6)
            last_end = t_cxl
            lvl_up = s.level if s.side == "Up" else 1 - s.level
            i0 = np.searchsorted(tts, t_post, side="right")
            i1 = np.searchsorted(tts, t_cxl, side="right")
            q = QUEUE_TYPICAL
            filled = 0.0
            t_fill = -1
            for i in range(i0, i1):
                if s.side == "Up":
                    if tbuy[i]:
                        continue
                    if tpx[i] < lvl_up - 1e-9:
                        take = min(SIZE - filled, tsz[i])
                    elif abs(tpx[i] - lvl_up) <= 1e-9:
                        beyond = max(0.0, tsz[i] - q)
                        q = max(0.0, q - tsz[i])
                        take = min(SIZE - filled, beyond)
                    else:
                        continue
                else:
                    if not tbuy[i]:
                        continue
                    if tpx[i] > lvl_up + 1e-9:
                        take = min(SIZE - filled, tsz[i])
                    elif abs(tpx[i] - lvl_up) <= 1e-9:
                        beyond = max(0.0, tsz[i] - q)
                        q = max(0.0, q - tsz[i])
                        take = min(SIZE - filled, beyond)
                    else:
                        continue
                if take > 0:
                    filled += take
                    if t_fill < 0:
                        t_fill = tts[i]
                if filled >= SIZE - 1e-9:
                    break
            if filled > 0:
                won = (s.side == "Up" and result == 0) or \
                      (s.side == "Down" and result == 1)
                pnl = filled * ((1.0 if won else 0.0) - s.level)
                fills.append((slug, s.t0_us, s.side, s.level, filled, won,
                              pnl, s.fv, s.tau_s))
    if not fills:
        print("NO FILLS")
        return
    F = pd.DataFrame(fills, columns=["slug", "t0_us", "side", "level",
                                     "shares", "won", "pnl", "fv", "tau_s"])
    per_mkt = F.groupby("slug").agg(pnl=("pnl", "sum"),
                                    stake=("shares", lambda s: np.sum(s)),
                                    n=("pnl", "size"))
    stake_d = (F.shares * F.level).groupby(F.slug).sum()
    per_mkt["stake_$"] = stake_d
    rng = np.random.default_rng(9)
    bs = [np.mean(rng.choice(per_mkt.pnl.values, len(per_mkt)))
          for _ in range(2000)]
    lo, hi = np.quantile(bs, [0.025, 0.975])
    tot_sh = F.shares.sum()
    print(f"\nfills: {len(F)} on {F.slug.nunique()} markets; "
          f"{tot_sh:.0f} shares, ${(F.shares*F.level).sum():.0f} stake")
    print(f"win rate (share-w): {np.average(F.won, weights=F.shares):.4f}  "
          f"avg entry {np.average(F.level, weights=F.shares):.4f}")
    print(f"PnL total ${F.pnl.sum():.2f}; per share "
          f"{100*F.pnl.sum()/tot_sh:.2f}c")
    print(f"per-market mean PnL ${per_mkt.pnl.mean():.3f} "
          f"CI [${lo:.3f}, ${hi:.3f}] over {len(per_mkt)} mkts")
    print(f"return on stake: {100*F.pnl.sum()/(F.shares*F.level).sum():.2f}%")
    F["month"] = pd.to_datetime(F.t0_us, unit="us").dt.to_period("M")
    print("\nby month:")
    print(F.groupby("month").agg(mkts=("slug", "nunique"),
                                 pnl=("pnl", "sum"),
                                 sh=("shares", "sum")).to_string())
    print("\nby side:")
    print(F.groupby("side").agg(pnl=("pnl", "sum"), sh=("shares", "sum"),
                                win=("won", "mean")).round(3).to_string())
    # concentration
    p = np.sort(per_mkt.pnl.values)
    print(f"\nconcentration: top-10 mkts = "
          f"{p[-10:].sum() / max(p.sum(), 1e-9):.2f} of total PnL")


if __name__ == "__main__":
    horizon = sys.argv[1]
    edge_min = float(sys.argv[2]) if len(sys.argv) > 2 else 0.03
    ttl = float(sys.argv[3]) if len(sys.argv) > 3 else 20.0
    run(horizon, edge_min, ttl, use_test="test" in sys.argv)
