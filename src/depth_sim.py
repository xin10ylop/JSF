"""Ex-ante execution test against RECORDED order books.

The tape test proves the mispricing exists at prices where trades cleared.
This one asks the harder question the brief demands: was the depth actually
resting there, at that price, at that moment, for us to take — with no
conditioning on a print having occurred.

Simulation (one decision per market, exactly what the bot would do):
  * walk the recorded book snapshots for a market in its final `window` sec
  * at each snapshot compute z from causal Binance 1s data
  * the FIRST time |z| >= zmin and the favoured side's best ask is priced
    below its modelled fair by `edge_min`, take up to `size` shares from the
    resting ask levels, paying the real taker fee
  * hold to settlement; P&L = 1{favoured won} - avg_fill - fee

Fill rule is deliberately conservative: we consume only size that was
visibly resting at that instant, and never more than the top-3 levels the
recorder captured.
"""
import argparse
import glob
import json
import os

import numpy as np
import pandas as pd
from scipy.stats import norm

from rollavg_edge_test import load_1s, CHANGE

FEE = 0.07
W = 30


class Px:
    """Causal 1s price series with prefix sums (index = close time)."""

    def __init__(self, coin, days):
        s = load_1s(coin, days)
        if s is None:
            raise ValueError("no klines")
        s = s.copy()
        s.index = s.index + 1                  # index by close time
        self.lo = int(s.index[0])
        self.v = s.values.astype("float64")
        self.cs = np.concatenate([[0.0], np.cumsum(self.v)])
        r = np.log(s / s.shift(1))
        self.sig = r.rolling(3600, min_periods=600).std().values

    def spot(self, t):
        i = t - self.lo
        return self.v[i] if 0 <= i < len(self.v) else np.nan

    def sigma(self, t):
        i = t - self.lo
        if not (0 <= i < len(self.sig)):
            return np.nan
        return self.sig[i] * self.v[i]

    def csum(self, a, b):
        ia = max(min(a - self.lo, len(self.v)), 0)
        ib = max(min(b - self.lo, len(self.v)), 0)
        return self.cs[ib] - self.cs[ia], ib - ia


def zscore(px, t0, t1, tau):
    ks, kn = px.csum(t0 - W, t0)
    if kn < W - 2:
        return None
    K = ks / kn
    S, _ = px.csum(t1 - W, tau)
    rem = t1 - tau
    sp = px.spot(tau)
    sg = px.sigma(tau)
    if not np.isfinite(sp) or not np.isfinite(sg) or sg <= 0 or rem <= 0:
        return None
    M = S + rem * sp - W * K
    sd = sg * np.sqrt(rem ** 3 / 3.0)
    return M / sd if sd > 0 else None


def run(a):
    meta = pd.concat([pd.read_parquet(f) for f in
                      sorted(glob.glob(f"data/pmfree/meta/btc_5m_*.parquet"))],
                     ignore_index=True).drop_duplicates("slug")
    meta = meta[meta.t0 >= (CHANGE if a.era == "post" else 0)]
    if a.era == "pre":
        meta = meta[meta.t0 < CHANGE]
    tok = {}
    for r in meta.itertuples():
        tok[str(r.tok_up)] = (r.slug, True, r.t0, r.t1, r.up_win)
        tok[str(r.tok_dn)] = (r.slug, False, r.t0, r.t1, r.up_win)

    files = sorted(glob.glob("data/live/clob/*.jsonl"))
    if a.days:
        files = [f for f in files if any(d in f for d in a.days)]
    # per market: best ask seen on each token, keyed by second
    snaps = {}
    nread = 0
    for f in files:
        with open(f) as fh:
            for line in fh:
                nread += 1
                if '"book"' not in line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if e.get("event_type") != "book":
                    continue
                aid = str(e.get("asset_id"))
                m = tok.get(aid)
                if m is None:
                    continue
                slug, is_up, t0, t1, up_win = m
                ts = int(e["timestamp"]) // 1000
                rel = ts - t0
                if not (300 - a.window <= rel < 300):
                    continue
                asks = [(float(x["price"]), float(x["size"]))
                        for x in e.get("asks", [])]
                asks = sorted([x for x in asks if 0 < x[0] < 1])
                if not asks:
                    continue
                snaps.setdefault((slug, t0, t1, up_win), {}).setdefault(
                    ts, {})[is_up] = asks
    print(f"read {nread:,} lines; {len(snaps):,} post-change markets with "
          f"book snapshots in the final {a.window}s")
    if not snaps:
        return

    days = sorted({pd.to_datetime(k[1], unit="s", utc=True).strftime("%Y-%m-%d")
                   for k in snaps})
    px = Px("btc", days)

    trades = []
    for (slug, t0, t1, up_win), bysec in sorted(snaps.items()):
        done = False
        for ts in sorted(bysec):
            if done:
                break
            z = zscore(px, t0, t1, ts)
            if z is None or abs(z) < a.zmin:
                continue
            want_up = z > 0
            asks = bysec[ts].get(want_up)
            if not asks:
                continue
            fair = float(norm.cdf(abs(z)))
            need = a.size
            cost = 0.0
            got = 0.0
            for p, sz in asks:
                if p >= fair - a.edge_min or p >= a.max_px:
                    break
                take = min(sz, need - got)
                if take <= 0:
                    break
                cost += take * p
                got += take
                if got >= need:
                    break
            if got <= 0:
                continue
            avg = cost / got
            won = (up_win if want_up else (not up_win))
            pnl = got * (float(won) - avg - FEE * avg * (1 - avg))
            trades.append({"slug": slug, "ts": ts, "rel": ts - t0, "z": z,
                           "side": "Up" if want_up else "Dn", "fair": fair,
                           "avg_px": avg, "shares": got, "won": float(won),
                           "pnl": pnl,
                           "day": pd.to_datetime(t0, unit="s", utc=True)
                           .strftime("%Y-%m-%d")})
            done = True
    if not trades:
        print("no qualifying fills")
        return
    d = pd.DataFrame(trades)
    print(f"\ntrades={len(d)}  markets_with_books={len(snaps)}  "
          f"take_rate={len(d)/len(snaps):.0%}")
    print(f"shares/trade avg {d.shares.mean():.0f} (requested {a.size})  "
          f"full-size fills {100*(d.shares>=a.size).mean():.0f}%")
    print(f"avg fill px {d.avg_px.mean():.4f}  hit {d.won.mean():.4f}  "
          f"avg |z| {d.z.abs().mean():.2f}  avg entry rel {d.rel.mean():.0f}s")
    ev = d.pnl.sum() / d.shares.sum()
    print(f"TOTAL P&L ${d.pnl.sum():,.2f} on {d.shares.sum():,.0f} shares "
          f"= {ev*100:+.2f}c/share")
    print(f"  per-trade mean ${d.pnl.mean():+.2f}  "
          f"SE ${d.pnl.sem():.2f}  t={d.pnl.mean()/max(d.pnl.sem(),1e-9):+.2f}")
    print("\nby day:")
    print(d.groupby("day").agg(n=("pnl", "size"), sh=("shares", "sum"),
                               hit=("won", "mean"), pnl=("pnl", "sum"))
          .to_string(float_format=lambda v: f"{v:,.2f}"))
    print("\nsample trades:")
    print(d.head(12)[["slug", "rel", "z", "side", "fair", "avg_px",
                      "shares", "won", "pnl"]].to_string(
        index=False, float_format=lambda v: f"{v:,.3f}"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=60)
    ap.add_argument("--zmin", type=float, default=1.5)
    ap.add_argument("--edge-min", type=float, default=0.02)
    ap.add_argument("--size", type=float, default=100)
    ap.add_argument("--max-px", type=float, default=0.97)
    ap.add_argument("--era", default="post", choices=["post", "pre"])
    ap.add_argument("--days", nargs="*", default=None)
    a = ap.parse_args()
    run(a)


if __name__ == "__main__":
    main()
