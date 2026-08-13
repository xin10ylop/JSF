"""Did the venue's book re-price the 2026-08-07 settlement change?

DEPRECATION WARNING (audit 2026-08-12, leakage review): the rule tested
below uses K over [t0, t0+w) -- the abandoned FORWARD-strike reading,
which scores ~0.89 against the venue -- NOT the verified trailing strike
mean[t0-w, t0) (0.9503) that the live bot implements. Its "NEW rule"
column therefore looks like a refutation of the live contract and is
not. It also lacks the +1 close-time kline re-index every other consumer
applies. Use src/verify_rule_multicoin.py for the contract; this file is
kept only because load_1s is imported elsewhere.

On 2026-08-07 Polymarket switched the 5m/15m crypto up/down contracts from
two instantaneous Chainlink prints to a rolling average:

    Up  iff  mean(P over [T-w, T])  >=  mean(P over [t0, t0+w])
    w = 30s for 5m markets, 60s for 15m

confirmed independently here by the resolutionSource field flipping from
'<coin>-usd' to '<coin>-usd-twap-30s-streams' on exactly that date for all
five coins.

Step 1 VERIFY the rule on the settled outcomes, against the old rule, using
       free Binance 1s klines as the price path.
Step 2 PRICE it. Inside the settle window the outcome is progressively
       locked: with n of w seconds already averaged, the remaining
       uncertainty shrinks like (w-n)/w. A book still quoting the old
       terminal contract is wrong in a measurable, directional way.
Step 3 TRADE it, against real prints only: for every print in the tape we
       compute the correct fair at that instant and take the side the model
       says is cheap, paying the real taker fee.

No fill assumptions: every price used is a price at which a trade happened.
"""
import argparse
import glob
import io
import zipfile

import numpy as np
import pandas as pd
from scipy.stats import norm

FEE = 0.07
SYM = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT",
       "xrp": "XRPUSDT", "doge": "DOGEUSDT"}
CHANGE = 1786060800  # 2026-08-07 00:00 UTC


def load_1s(coin, days):
    parts = []
    for d in days:
        f = f"data/binance_alts/{SYM[coin]}-1s-{d}.zip"
        try:
            with zipfile.ZipFile(f) as z:
                raw = z.read(z.namelist()[0])
        except Exception:  # noqa: BLE001
            continue
        head = raw[:200].decode("utf8", "ignore").lower()
        hdr = 0 if "open_time" in head else None
        x = pd.read_csv(io.BytesIO(raw), header=hdr, usecols=[0, 4])
        x.columns = ["ot", "close"]
        parts.append(x)
    if not parts:
        return None
    x = pd.concat(parts, ignore_index=True)
    x["ot"] = pd.to_numeric(x.ot, errors="coerce")
    x["close"] = pd.to_numeric(x.close, errors="coerce")
    x = x.dropna()
    sec = np.where(x.ot > 1e14, x.ot // 1_000_000, x.ot // 1000).astype("int64")
    s = pd.Series(x["close"].values, index=sec).groupby(level=0).last()
    full = pd.RangeIndex(s.index.min(), s.index.max() + 1)
    return s.reindex(full).ffill()


def window_stats(px, t0, t1, w):
    """Vectorised means over [t0,t0+w) and [t1-w,t1) via a prefix sum."""
    idx = px.index
    v = px.values.astype("float64")
    cs = np.concatenate([[0.0], np.cumsum(v)])
    lo = idx[0]

    def mean(a, b):
        ia = np.clip(a - lo, 0, len(v))
        ib = np.clip(b - lo, 0, len(v))
        n = ib - ia
        out = np.where(n > 0, (cs[ib] - cs[ia]) / np.maximum(n, 1), np.nan)
        return out, n
    k, nk = mean(t0, t0 + w)
    r, nr = mean(t1 - w, t1)
    p0 = np.where((t0 - lo >= 0) & (t0 - lo < len(v)), v[np.clip(t0 - lo, 0, len(v) - 1)], np.nan)
    p1 = np.where((t1 - lo >= 0) & (t1 - lo < len(v)), v[np.clip(t1 - lo, 0, len(v) - 1)], np.nan)
    return k, r, p0, p1, nk, nr


def verify(coins, w=30):
    print("=" * 96)
    print("STEP 1 — verify the settlement rule against settled outcomes")
    print("=" * 96)
    print(f"{'coin':>5} {'period':>22} {'n':>6} {'NEW rule':>9} {'OLD rule':>9} "
          f"{'disagree':>9} {'NEW|disagree':>13}")
    out = []
    for coin in coins:
        mf = sorted(glob.glob(f"data/pmfree/meta/{coin}_5m_*.parquet"))
        m = pd.concat([pd.read_parquet(f) for f in mf], ignore_index=True)
        m = m[(m.t0 >= 1785484800) & (m.t0 < 1786320000)]   # Aug 1..Aug 9
        days = sorted(pd.to_datetime(m.t0, unit="s", utc=True)
                      .dt.strftime("%Y-%m-%d").unique())
        px = load_1s(coin, days)
        if px is None:
            continue
        k, r, p0, p1, nk, nr = window_stats(px, m.t0.values, m.t1.values, w)
        ok = (~np.isnan(k)) & (~np.isnan(r)) & (nk >= w - 2) & (nr >= w - 2)
        d = m.assign(new=(r >= k), old=(p1 >= p0), ok=ok)
        d = d[d.ok]
        for lbl, sub in (("pre  (Aug1-6)", d[d.t0 < CHANGE]),
                         ("post (Aug7-9)", d[d.t0 >= CHANGE])):
            if not len(sub):
                continue
            dis = sub[sub.new != sub.old]
            print(f"{coin:>5} {lbl:>22} {len(sub):>6,} "
                  f"{(sub.new==sub.up_win).mean():>9.4f} "
                  f"{(sub.old==sub.up_win).mean():>9.4f} "
                  f"{len(dis):>9,} "
                  f"{((dis.new==dis.up_win).mean() if len(dis) else np.nan):>13.4f}")
            out.append((coin, lbl, len(sub), (sub.new == sub.up_win).mean(),
                        (sub.old == sub.up_win).mean(), len(dis)))
    return out


def fair_rollavg(spot, K, R, n_done, T, t, w, sigma):
    """P(Up) for the rolling-average contract.

    Before the settle window opens (t <= T-w): the average of the last w
    seconds around a driftless walk has sd sigma*sqrt(s + w/3) with
    s = (T-w)-t, so P(Up) = Phi((spot-K)/sd).
    Inside it: R is the sum of the n_done seconds already printed; the
    remaining rem = T-t seconds contribute mean spot with variance
    sigma^2*rem^3/3, and the target is K*w - R.
    """
    out = np.full(len(spot), np.nan)
    rem = T - t
    pre = t <= (T - w)
    s = np.maximum((T - w) - t, 0)
    sd = sigma * np.sqrt(np.maximum(s + w / 3.0, 1e-9))
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(pre & (sd > 0), norm.cdf((spot - K) / sd), out)
        num = R + spot * rem - K * w
        sd2 = sigma * np.sqrt(np.maximum(rem ** 3 / 3.0, 1e-12))
        out = np.where((~pre) & (sd2 > 0), norm.cdf(num / sd2), out)
    return np.clip(out, 0.0, 1.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", nargs="+", default=["btc", "eth", "sol", "xrp", "doge"])
    ap.add_argument("--root", default="data/pmfree_aug")
    ap.add_argument("--w", type=int, default=30)
    ap.add_argument("--last", type=int, default=90, help="seconds before t1")
    ap.add_argument("--edge", type=float, default=0.05)
    a = ap.parse_args()
    verify(a.coins, a.w)


if __name__ == "__main__":
    main()
