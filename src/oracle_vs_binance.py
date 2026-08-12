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
import time

import numpy as np
import requests

CHANGE = 1786060800          # 2026-08-07 00:00 UTC, the contract change
GAMMA = "https://gamma-api.polymarket.com/markets"
KLINES = "https://data-api.binance.vision/api/v3/klines"
BSYM = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT",
        "xrp": "XRPUSDT", "doge": "DOGEUSDT"}
UA = {"User-Agent": "Mozilla/5.0"}


def binance_1s(symbol, lo_s, hi_s):
    """{second: close} from the public mirror, paged forward.

    Fetched live rather than read from data/binance_alts so this runs on
    the TRADING host, which is where the oracle ticks are. The droplet has
    no research datasets -- data/ is gitignored -- so the offline version
    of this script could only ever run on a dev box that had no oracle
    capture. That is exactly backwards.
    """
    out, cur = {}, int(lo_s) * 1000
    end = int(hi_s) * 1000
    s = requests.Session()
    s.headers.update(UA)
    while cur < end:
        try:
            r = s.get(KLINES, params={"symbol": symbol, "interval": "1s",
                                      "startTime": cur, "limit": 1000},
                      timeout=30)
            if r.status_code != 200:
                break
            arr = r.json()
        except Exception:  # noqa: BLE001
            break
        if not arr:
            break
        for k in arr:
            # klines are stamped by OPEN time; the close is known one
            # second later, so index by close time to stay causal
            out[int(k[0]) // 1000 + 1] = float(k[4])
        cur = int(arr[-1][0]) + 1000
    return out


def outcomes(slugs):
    """slug -> True if Up won, from the venue's settled outcomePrices."""
    res, s = {}, requests.Session()
    s.headers.update(UA)
    slugs = sorted(set(slugs))
    for i in range(0, len(slugs), 100):
        params = [("slug", x) for x in slugs[i:i + 100]]
        params += [("closed", "true"), ("limit", "500")]
        try:
            r = s.get(GAMMA, params=params, timeout=45)
            if r.status_code != 200:
                continue
            for mk in r.json():
                op, oc = mk.get("outcomePrices"), mk.get("outcomes")
                if isinstance(op, str):
                    op = json.loads(op)
                if isinstance(oc, str):
                    oc = json.loads(oc)
                if not op:
                    continue
                iu = oc.index("Up") if oc and "Up" in oc else 0
                res[mk["slug"]] = float(op[iu]) > 0.5
        except Exception:  # noqa: BLE001
            continue
    return res


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
    # Klines come one 1000-bar request per ~17 minutes of 1s data, so an
    # unbounded window pages for many minutes. Cap it at the most recent
    # slice of oracle coverage, which is also the freshest.
    ap.add_argument("--hours", type=float, default=12.0,
                    help="only the last N hours of oracle coverage")
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
    lo_s = max(lo_s, hi_s - int(a.hours * 3600))
    orc = {k: v for k, v in orc.items() if k >= lo_s}
    fmt = "%Y-%m-%d %H:%M"
    print(f"{osym}: {len(orc):,} ticks in the last {a.hours:g}h, spanning "
          f"{time.strftime(fmt, time.gmtime(lo_s))} .. "
          f"{time.strftime(fmt, time.gmtime(hi_s))} UTC")

    # Every market boundary inside the recorded oracle window.
    step = 300 if a.fam == "5m" else 900
    cand = [t for t in range(((int(lo_s) + w) // step + 1) * step,
                             int(hi_s) - step, step) if t >= CHANGE]
    if not cand:
        print("no post-change market boundaries inside the oracle window")
        return
    print(f"{len(cand)} candidate {a.coin} {a.fam} markets in that window; "
          f"fetching outcomes and klines ...")
    res = outcomes([f"{a.coin}-updown-{a.fam}-{t}" for t in cand])
    bser = binance_1s(BSYM[a.coin], lo_s - w - 5, hi_s + 5)
    if not bser:
        print("could not fetch Binance klines for the window")
        return

    rows = []
    for t0 in cand:
        slug = f"{a.coin}-updown-{a.fam}-{t0}"
        if slug not in res:
            continue
        t1, won = t0 + step, bool(res[slug])
        r = type("R", (), {"slug": slug})
        ok = mean_from(orc, t0 - w, t0), mean_from(orc, t1 - w, t1)
        bk = mean_from(bser, t0 - w, t0), mean_from(bser, t1 - w, t1)
        if None in ok or None in bk:
            continue
        rows.append({"slug": slug, "won": won,
                     "orc": ok[1] >= ok[0], "bnc": bk[1] >= bk[0],
                     "orc_margin": ok[1] - ok[0], "bnc_margin": bk[1] - bk[0]})
    import pandas as pd
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
