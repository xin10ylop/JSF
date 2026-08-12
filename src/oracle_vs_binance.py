"""How much signal does the tape LOSE by pricing off Binance?

The contract settles on a Chainlink TWAP stream. The live bot reads that
stream. But every offline measurement in this project -- tape_latency,
tape_capacity, endgame_rollavg -- builds K, S, spot and sigma from Binance
1s klines, because that is what exists for months back.

If Binance is a noisy proxy for Chainlink, the offline z is a noisy version
of the live z, and every tape figure is a LOWER BOUND on what the bot can
do. That is the leading unquantified explanation for live beating the tape,
and it has been asserted rather than measured.

This measures it directly, on the hours where bot/recorder.py captured real
Chainlink ticks. It applies the SAME verified contract to both series and
asks which one agrees with the venue's own settled outcome:

    Up  iff  mean(P over [t1-w, t1))  >=  mean(P over [t0-w, t0))

Every market scored is one where both series have full coverage, so the
comparison is paired -- same markets, same rule, different input.

    python3 src/oracle_vs_binance.py --coin btc
"""
import argparse
import glob
import json
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "src")
from rollavg_edge_test import load_1s, CHANGE  # noqa: E402


def oracle_series(symbol):
    """{unix_second: price} from the recorder's RTDS capture."""
    out = {}
    for f in sorted(glob.glob("data/live/rtds/*.jsonl")):
        try:
            fh = open(f)
        except OSError:
            continue
        with fh:
            for line in fh:
                if symbol not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                p = d.get("payload") or {}
                if p.get("symbol") != symbol:
                    continue
                try:
                    out[int(p["timestamp"]) // 1000] = float(p["value"])
                except (KeyError, TypeError, ValueError):
                    continue
    return out


def mean_from(series, a, b):
    """Mean of a {second: price} map over [a, b), None if under-covered.

    The oracle misses ~a third of seconds, so require half the window
    rather than every second -- but never extrapolate across a real gap.
    """
    vals = [series[s] for s in range(int(a), int(b)) if s in series]
    if len(vals) < (b - a) / 2:
        return None
    return sum(vals) / len(vals)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", default="btc")
    ap.add_argument("--fam", default="5m")
    a = ap.parse_args()
    w = 30 if a.fam == "5m" else 60

    osym = f"{a.coin}/usd"
    orc = oracle_series(osym)
    if len(orc) < 1000:
        print(f"only {len(orc)} {osym} oracle ticks on disk -- the recorder "
              f"filtered to btc until 2026-08-11, so other coins have "
              f"almost nothing. Nothing to measure.")
        return
    lo_s, hi_s = min(orc), max(orc)
    print(f"{osym}: {len(orc):,} ticks spanning "
          f"{pd.to_datetime(lo_s, unit='s')} .. {pd.to_datetime(hi_s, unit='s')}")

    mf = sorted(glob.glob(f"data/pmfree/meta/{a.coin}_{a.fam}_*.parquet"))
    m = pd.concat([pd.read_parquet(f) for f in mf],
                  ignore_index=True).drop_duplicates("slug")
    m = m[(m.t0 >= CHANGE)].dropna(subset=["t0", "t1", "up_win"])
    m = m[(m.t0 - w >= lo_s) & (m.t1 <= hi_s)]
    if not len(m):
        print("no post-change markets inside the recorded oracle window")
        return

    days = sorted(pd.to_datetime(m.t0, unit="s", utc=True)
                  .dt.strftime("%Y-%m-%d").unique())
    px = load_1s(a.coin, days)
    px = px.copy()
    px.index = px.index + 1               # klines stamped by OPEN time
    bser = {int(k): float(v) for k, v in px.items()}

    rows = []
    for _, r in m.iterrows():
        t0, t1, won = int(r.t0), int(r.t1), bool(r.up_win)
        ok = mean_from(orc, t0 - w, t0), mean_from(orc, t1 - w, t1)
        bk = mean_from(bser, t0 - w, t0), mean_from(bser, t1 - w, t1)
        if None in ok or None in bk:
            continue
        rows.append({"slug": r.slug, "won": won,
                     "orc": ok[1] >= ok[0], "bnc": bk[1] >= bk[0],
                     "orc_margin": ok[1] - ok[0], "bnc_margin": bk[1] - bk[0]})
    d = pd.DataFrame(rows)
    if len(d) < 8:
        print(f"only {len(d)} markets with full coverage on BOTH series")
        return
    if len(d) < 40:
        print(f"\n  !! INDICATIVE ONLY: {len(d)} markets. The recorder "
              f"filtered RTDS to btc and ran in scattered hours, so this is "
              f"all the paired coverage that exists. From 2026-08-11 it "
              f"records all five coins continuously; re-run in a day for a "
              f"real sample.")

    print(f"\n{len(d)} post-change {a.coin} {a.fam} markets with full "
          f"coverage on both series\n")
    print(f"  rule computed from CHAINLINK  agrees with the venue "
          f"{(d.orc == d.won).mean():.4f}")
    print(f"  rule computed from BINANCE    agrees with the venue "
          f"{(d.bnc == d.won).mean():.4f}")
    dis = d[d.orc != d.bnc]
    print(f"\n  the two series disagree on {len(dis)} of {len(d)} markets "
          f"({len(dis)/len(d):.1%})")
    if len(dis):
        print(f"  on those, CHAINLINK is right {(dis.orc == dis.won).mean():.3f} "
              f"of the time")
    # The signal the strategy trades is the MARGIN, not the sign. If Binance
    # tracks the margin closely the proxy is cheap; if not, every offline z
    # is attenuated and the tape understates the edge.
    c = np.corrcoef(d.orc_margin, d.bnc_margin)[0, 1]
    print(f"\n  margin correlation between the two series: {c:.4f}")
    print(f"  sd of (binance margin - chainlink margin): "
          f"{(d.bnc_margin - d.orc_margin).std():.4f} "
          f"vs chainlink margin sd {d.orc_margin.std():.4f}")
    print("\n  A proxy that disagrees about the OUTCOME on x% of markets "
          "cannot\n  price z as sharply as the series that settles them. "
          "That gap is\n  the tape's handicap, and it is why every tape "
          "figure is a floor.")


if __name__ == "__main__":
    main()
