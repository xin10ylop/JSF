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
import sys

import numpy as np
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rollavg_edge_test import load_1s, CHANGE
sys_path_hack = None
from bot.calib import p_up

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

    # Book evidence lives in TWO places: hours already distilled by
    # bot/prune.py into data/live/books/*.parquet, and any recent hour still
    # sitting as raw JSONL. The pruner DELETES the raw file once distilled,
    # so reading only data/live/clob/*.jsonl silently finds nothing on a
    # long-running box -- which is exactly the machine this test is for.
    snaps = {}
    nread = 0

    def add(aid, ts, asks, seq):
        """seq orders updates WITHIN a second.

        The recorder sees a mean of 4.77 book updates per token-second (p90
        12, max 90). Keying snapshots by whole second and overwriting keeps
        only the last one and discards 79% of them -- which silently
        simulates 1-second polling rather than the bot's event-driven
        evaluation, and understates the take rate.
        """
        m = tok.get(str(aid))
        if m is None:
            return
        slug, is_up, t0, t1, up_win = m
        rel = ts - t0
        if not (300 - a.window <= rel < 300):
            return
        asks = sorted([(p, s) for p, s in asks if 0 < p < 1])
        if not asks:
            return
        snaps.setdefault((slug, t0, t1, up_win), []).append(
            (int(seq), ts, is_up, asks))

    pq = sorted(glob.glob("data/live/books/*.parquet"))
    if a.days:
        pq = [f for f in pq if any(d in f for d in a.days)]
    for f in pq:
        try:
            d = pd.read_parquet(f)
        except Exception:  # noqa: BLE001
            continue
        for r in d.itertuples():
            nread += 1
            add(r.asset_id, int(r.ts),
                list(zip([float(x) for x in r.ask_px],
                         [float(x) for x in r.ask_sz])),
                int(r.t_local_us))

    raw = sorted(glob.glob("data/live/clob/*.jsonl"))
    if a.days:
        raw = [f for f in raw if any(d in f for d in a.days)]
    for f in raw:
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
                add(e.get("asset_id"), int(e["timestamp"]) // 1000,
                    [(float(x["price"]), float(x["size"]))
                     for x in e.get("asks", [])],
                    e.get("t_local_us") or int(e["timestamp"]) * 1000)
    print(f"read {nread:,} records from {len(pq)} distilled + {len(raw)} raw "
          f"files; {len(snaps):,} post-change markets with book snapshots "
          f"in the final {a.window}s")
    if not snaps:
        return

    days = sorted({pd.to_datetime(k[1], unit="s", utc=True).strftime("%Y-%m-%d")
                   for k in snaps})
    px = Px("btc", days)

    trades = []
    for (slug, t0, t1, up_win), evs in sorted(snaps.items()):
        evs.sort()                       # by arrival time within the market
        latest = {}                      # token -> current asks, as the bot holds
        done = False
        for seq, ts, is_up, asks in evs:
            latest[is_up] = asks
            if done:
                break
            z = zscore(px, t0, t1, ts)
            if z is None or abs(z) < a.zmin:
                continue
            want_up = z > 0
            asks_f = latest.get(want_up)
            if not asks_f:
                continue
            fair = float(p_up(abs(z)))
            need, cost, got = a.size, 0.0, 0.0
            for p, sz in asks_f:
                if p > a.max_px or (a.require_edge and p >= fair - a.edge_min):
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
    # Defaults MIRROR bot/config.json's validated gate: the settle window
    # only (w=30s on 5m), |z|>=2.0, any ask at or below max_price.
    ap.add_argument("--window", type=int, default=30)
    ap.add_argument("--zmin", type=float, default=2.0)
    ap.add_argument("--require-edge", action="store_true",
                    help="also demand empirical_fair - ask >= edge_min "
                         "(the untested longshot variant, off in the bot)")
    ap.add_argument("--edge-min", type=float, default=0.02)
    ap.add_argument("--size", type=float, default=100)
    ap.add_argument("--max-px", type=float, default=0.97)
    ap.add_argument("--era", default="post", choices=["post", "pre"])
    ap.add_argument("--days", nargs="*", default=None)
    a = ap.parse_args()
    run(a)


if __name__ == "__main__":
    main()
