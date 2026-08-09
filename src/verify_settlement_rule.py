"""Resolve WHAT the btc-updown settlement rule became on 2026-08-07.

On 2026-08-07 every btc-updown 5m/15m market description switched from
  "the Bitcoin price at the end of the time range"
to
  "the time-weighted average price (TWAP) of Bitcoin ... of the time range".

Binance-proxy evidence (2 days) says end-price STILL governs outcomes
(79% correct on rule-disagreement markets vs an 88% pre-change baseline),
but the overall end-price match rate fell 0.954 -> 0.897 (~5 sigma), so
something did change and the proxy cannot identify it.

This script settles it using REAL Chainlink ticks captured by
bot/recorder.py (data/live/rtds/*.jsonl). Run it once a few complete
market windows have been recorded post-change; it needs no API quota.

Why it matters: if settlement is an average, the correct pricer scores
Brier ~0.002 late in a window where a terminal pricer scores ~0.108 and
the market historically scored ~0.042 (see src/twap_pricer.py).

Usage: python3 src/verify_settlement_rule.py
"""
import glob
import json

import numpy as np
import pandas as pd

CHANGE_TS = 1786060800   # 2026-08-07 00:00 UTC


def load_oracle():
    ticks = []
    for f in sorted(glob.glob("data/live/rtds/*.jsonl")):
        for line in open(f):
            try:
                d = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            p = d.get("payload", {})
            if p.get("symbol") != "btc/usd":
                continue
            try:
                ticks.append((int(p["timestamp"]) // 1000, float(p["value"])))
            except Exception:  # noqa: BLE001
                continue
    if not ticks:
        return None
    s = pd.DataFrame(ticks, columns=["sec", "px"]).groupby("sec").px.last()
    return s.sort_index()


def main():
    s = load_oracle()
    if s is None or len(s) < 600:
        print("not enough recorded oracle data yet — keep bot/recorder.py running")
        return
    # contiguous runs of coverage (gap > 5s splits a run)
    idx = s.index.values
    brk = np.where(np.diff(idx) > 5)[0]
    runs = np.split(np.arange(len(idx)), brk + 1)
    mk = pd.read_parquet("data/telonex/markets_fresh.parquet") \
        if glob.glob("data/telonex/markets_fresh.parquet") \
        else pd.read_parquet("data/telonex/markets.parquet")
    ud = mk[mk.slug.str.match(r"btc-updown-(5m|15m)-\d+", na=False)].copy()
    ud["t0"] = ud.slug.str.extract(r"-(\d+)$")[0].astype("int64")
    ud["win"] = ud.slug.str.extract(r"btc-updown-(\d+m)-")[0].map(
        {"5m": 300, "15m": 900})
    ud["t1"] = ud.t0 + ud.win
    ud = ud[ud.result_id != ""].copy()
    ud["res"] = ud.result_id.astype(int)

    rows = []
    for r in runs:
        lo, hi = idx[r[0]], idx[r[-1]]
        if hi - lo < 300:
            continue
        gr = np.arange(lo, hi + 1)
        px = s.reindex(gr).ffill().values
        cum = np.concatenate([[0.0], np.cumsum(px)])
        sub = ud[(ud.t0 >= lo) & (ud.t1 <= hi)]
        for m in sub.itertuples():
            i0 = int(m.t0 - lo)
            i1 = int(m.t1 - lo)
            K = px[i0]
            end = px[min(i1, len(px) - 1)]
            twap = (cum[i1] - cum[i0]) / max(i1 - i0, 1)
            rows.append((m.slug, m.t0, m.res,
                         0 if end >= K else 1,
                         0 if twap >= K else 1,
                         "POST" if m.t0 >= CHANGE_TS else "PRE"))
    if not rows:
        print("recorded coverage does not yet contain a complete market window")
        return
    D = pd.DataFrame(rows, columns=["slug", "t0", "res", "pred_end",
                                    "pred_twap", "era"])
    print(f"complete windows with REAL oracle coverage: {len(D)}")
    for era, d in D.groupby("era"):
        e = (d.pred_end == d.res).mean()
        t = (d.pred_twap == d.res).mean()
        dis = d[d.pred_end != d.pred_twap]
        print(f"{era}: n={len(d)}  END {e:.3f}  TWAP {t:.3f}  "
              f"| disagreements n={len(dis)}"
              + (f", END correct {(dis.pred_end==dis.res).sum()}/{len(dis)}"
                 if len(dis) else ""))
    post = D[D.era == "POST"]
    if len(post) >= 30:
        e = (post.pred_end == post.res).mean()
        t = (post.pred_twap == post.res).mean()
        print("\nVERDICT:", "TWAP" if t > 0.97 >= e else
              ("END unchanged" if e > 0.97 else "NEITHER — investigate "
               "sub-window averages / changed anchor"))


if __name__ == "__main__":
    main()
