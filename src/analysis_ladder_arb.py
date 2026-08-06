"""H12: strike-ladder monotonicity violations in the hourly above family.

P(close > K_low) >= P(close > K_high) for K_low < K_high. A tradeable
violation is ask(Yes,K_low) < bid(Yes,K_high): buy the low-strike Yes at
ask, sell the high-strike Yes (buy its No at 1-bid) — a guaranteed >= 0
payoff spread bought for < 0 net. Net profit after taker fees on both legs.

Scans tape-implied quotes at 60s snapshots for all strikes sharing an
expiry hour. Reports frequency, size, and net-of-fee profitability.
"""
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "src")
from ladder_backtest import iter_tapes  # noqa: E402

FEE = 0.07


def main():
    m = pd.read_parquet("data/master_above.parquet")
    m = m[m.result >= 0]
    info = {r.slug: (r.strike, r.t1_us) for r in m.itertuples()}

    # collect snapshots per market: tape-implied bid/ask each 60s in the
    # final hour
    snaps = []
    for slug, tape in iter_tapes("above_hourly_trades"):
        if slug not in info:
            continue
        K, t1 = info[slug]
        t0 = t1 - 3600_000_000
        tts = tape.timestamp_us.values
        tpx = tape.price.values.astype("float64")
        tbuy = tape.is_buy.values
        gt = t0 + np.arange(300, 3600, 60, dtype="int64") * 1_000_000
        tb = tpx[tbuy]
        ts_ = tpx[~tbuy]
        ttb = tts[tbuy]
        tts_ = tts[~tbuy]
        if len(tb) == 0 or len(ts_) == 0:
            continue
        ia = np.searchsorted(ttb, gt, "right") - 1
        ib = np.searchsorted(tts_, gt, "right") - 1
        ask = np.where((ia >= 0) & ((gt - ttb[ia.clip(0)]) < 120e6),
                       tb[ia.clip(0)], np.nan)
        bid = np.where((ib >= 0) & ((gt - tts_[ib.clip(0)]) < 120e6),
                       ts_[ib.clip(0)], np.nan)
        for g, a, b in zip(gt, ask, bid):
            if np.isfinite(a) or np.isfinite(b):
                snaps.append((t1, K, g, b, a))
    S = pd.DataFrame(snaps, columns=["t1", "K", "t", "bid", "ask"])
    print(f"snapshots: {len(S)} across {S.t1.nunique()} expiry hours")

    # pairwise adjacent-strike checks at same (t1, t)
    viol = []
    for (t1, t), grp in S.groupby(["t1", "t"]):
        grp = grp.sort_values("K")
        if len(grp) < 2:
            continue
        for i in range(len(grp) - 1):
            lo = grp.iloc[i]
            hi = grp.iloc[i + 1]
            if np.isfinite(lo.ask) and np.isfinite(hi.bid) \
                    and lo.ask < hi.bid - 1e-9:
                gross = hi.bid - lo.ask
                fee = FEE * lo.ask * (1 - lo.ask) \
                    + FEE * (1 - hi.bid) * hi.bid
                viol.append((t1, t, lo.K, hi.K, lo.ask, hi.bid,
                             gross, gross - fee))
    V = pd.DataFrame(viol, columns=["t1", "t", "K_lo", "K_hi", "ask_lo",
                                    "bid_hi", "gross", "net"])
    print(f"monotonicity violations: {len(V)} "
          f"({100*len(V)/max(len(S),1):.3f}% of snapshots)")
    if len(V):
        print(f"gross: mean {100*V.gross.mean():.2f}c, "
              f"p90 {100*V.gross.quantile(0.9):.2f}c")
        print(f"net of taker fees: mean {100*V.net.mean():.2f}c; "
              f"positive: {(V.net > 0).mean():.2%} of violations")
        print(f"expiry hours affected: {V.t1.nunique()}")


if __name__ == "__main__":
    main()
