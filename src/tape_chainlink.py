"""The tape backtest with the CORRECT input series: z from Chainlink.

Every offline tape figure in this project prices z off Binance 1s klines,
because that is what exists months back. But the venue settles on the
Chainlink stream, and src/oracle_vs_binance.py measured the cost of that
proxy: the Binance-derived rule mis-signs 0.7-5.1% of markets depending on
coin, an implied ~2.7c/share drag, which is why live (+4.55c/sh) runs ~2.5x
above the Binance tape (~1.8c/sh).

This is the definitive test of that explanation. Same markets, same real
prints, same fee, same fill assumptions, same decision lag -- the ONLY
thing that changes is the input series for K, S, spot and sigma:

    BINANCE-z   what every prior tape figure used
    CHAINLINK-z what the live bot actually computes

Both are run side by side on the hours where bot/recorder.py captured real
oracle ticks, so the comparison is paired print-for-print. If CHAINLINK-z
converges to the live c/share per coin, the live-vs-tape gap is closed
mechanically. If it does not, something else is flattering the live run.

Run on the TRADING host (that is where data/live/rtds lives):

    python3 src/tape_chainlink.py --coins btc,eth,sol,xrp,doge --hours 24
"""
import argparse
import bisect
import glob
import json
import time
import concurrent.futures as cf

import numpy as np
import pandas as pd
import requests

CHANGE = 1786060800          # 2026-08-07 00:00 UTC, the contract change
GAMMA = "https://gamma-api.polymarket.com/markets"
TRADES = "https://data-api.polymarket.com/trades"
KLINES = "https://data-api.binance.vision/api/v3/klines"
BSYM = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT",
        "xrp": "XRPUSDT", "doge": "DOGEUSDT"}
UA = {"User-Agent": "Mozilla/5.0"}
FEE = 0.07
STEP = {"5m": 300, "15m": 900}
WIN = {"5m": 30, "15m": 60}


def clustered_t(pnl_per_sh, qty, slugs):
    """Share-weighted t clustered by market -- same as tape_capacity."""
    d = pd.DataFrame({"p": pnl_per_sh, "q": qty, "s": slugs})
    g = d.groupby("s").apply(lambda x: pd.Series(
        {"pnl": (x.p * x.q).sum(), "sh": x.q.sum()}), include_groups=False)
    mu = g.pnl.sum() / g.sh.sum()
    resid = g.pnl - mu * g.sh
    se = (resid ** 2).sum() ** 0.5 / g.sh.sum()
    return (mu / se if se > 0 else float("nan")), len(g)


class Series:
    """Irregular tick series with the bot's own accessor semantics."""

    def __init__(self, ts, px):
        self.ts = np.asarray(ts, dtype="float64")     # seconds, sorted
        self.px = np.asarray(px, dtype="float64")

    def spot(self, t, max_age=20.0):
        """Last price at or before t; None if older than max_age (the bot
        refuses to price on an oracle round staler than 20s)."""
        i = bisect.bisect_right(self.ts, t) - 1
        if i < 0 or t - self.ts[i] > max_age:
            return None
        return self.px[i]

    def integral(self, a, b):
        """Held-price integral over [a, b): (price_seconds, covered, nticks).

        Mirrors BotState._integral: each price is in force until the next
        tick; `covered` means a price existed at or before the window start.
        """
        if b <= a:
            return 0.0, False, 0
        i0 = bisect.bisect_right(self.ts, a) - 1
        j = bisect.bisect_left(self.ts, b)
        covered = i0 >= 0
        idx = range(max(i0, 0), j)
        pts = [(self.ts[k], self.px[k]) for k in idx if self.ts[k] < b]
        n_in = sum(1 for t, _ in pts if t > a)
        if not pts:
            return 0.0, False, 0
        cur_t = a
        cur_px = self.px[i0] if covered else pts[0][1]
        total = 0.0
        for t, px in pts:
            if t <= a:
                continue
            total += cur_px * (t - cur_t)
            cur_t, cur_px = t, px
        total += cur_px * (b - cur_t)
        return total, covered, n_in


def rolling_sigma(by_sec, lo, hi, window=3600, min_obs=300):
    """Per-second trailing sd of 1s log returns, adjacent pairs only --
    the bot's oracle_sigma_rel. Returns {second: sd}."""
    idx = np.arange(int(lo), int(hi) + 1)
    px = pd.Series([by_sec.get(int(s), np.nan) for s in idx], index=idx)
    r = np.log(px).diff()          # NaN across any gap: adjacent pairs only
    sd = r.rolling(window, min_periods=min_obs).std()
    return sd


def oracle_series(symbol, hours):
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
                    out[int(p["timestamp"]) / 1000.0] = float(p["value"])
                except (KeyError, TypeError, ValueError):
                    continue
    if not out:
        return None
    hi = max(out)
    lo = hi - hours * 3600
    ts = sorted(t for t in out if t >= lo)
    return Series(ts, [out[t] for t in ts])


def binance_by_sec(symbol, lo_s, hi_s):
    """{close_second: close} -- indexed by CLOSE time to stay causal."""
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
            out[int(k[0]) // 1000 + 1] = float(k[4])
        cur = int(arr[-1][0]) + 1000
    return out


def market_meta(slugs):
    """slug -> {up_win, cond_id, tok_up} from Gamma."""
    res, s = {}, requests.Session()
    s.headers.update(UA)
    slugs = sorted(set(slugs))
    for i in range(0, len(slugs), 100):
        params = [("slug", x) for x in slugs[i:i + 100]]
        params += [("closed", "true"), ("limit", "500")]
        for attempt in range(3):
            try:
                r = s.get(GAMMA, params=params, timeout=45)
                if r.status_code != 200:
                    time.sleep(1 + attempt)
                    continue
                for mk in r.json():
                    op, oc = mk.get("outcomePrices"), mk.get("outcomes")
                    tk = mk.get("clobTokenIds")
                    if isinstance(op, str):
                        op = json.loads(op)
                    if isinstance(oc, str):
                        oc = json.loads(oc)
                    if isinstance(tk, str):
                        tk = json.loads(tk)
                    if not op or not tk:
                        continue
                    iu = oc.index("Up") if oc and "Up" in oc else 0
                    res[mk["slug"]] = {"up_win": float(op[iu]) > 0.5,
                                       "cond_id": mk.get("conditionId"),
                                       "tok_up": str(tk[iu])}
                break
            except Exception:  # noqa: BLE001
                time.sleep(1 + attempt)
    return res


def fetch_trades(meta_items, workers=16):
    """[(slug, ts, p_up, size, is_ask)] via the free data-api tape."""

    def one(item):
        slug, mm = item
        out, off = [], 0
        s = requests.Session()
        s.headers.update(UA)
        while True:
            try:
                r = s.get(TRADES, params={"market": mm["cond_id"],
                                          "limit": 500, "offset": off},
                          timeout=45)
                if r.status_code != 200:
                    break
                js = r.json()
            except Exception:  # noqa: BLE001
                break
            if not js:
                break
            for x in js:
                if x.get("conditionId") != mm["cond_id"]:
                    continue
                px = float(x["price"])
                is_up = str(x.get("asset")) == mm["tok_up"]
                # is_ask: the print consumed an ASK on the side we would
                # buy -- an aggressive BUY of that token. Same construction
                # as tape_latency.
                is_ask_up = (is_up and x.get("side") == "BUY")
                is_ask_dn = ((not is_up) and x.get("side") == "BUY")
                out.append((slug, int(x["timestamp"]),
                            px if is_up else 1.0 - px,
                            float(x["size"]), is_ask_up, is_ask_dn))
            if len(js) < 500 or off >= 4000:
                break
            off += 500
        return out

    rows = []
    with cf.ThreadPoolExecutor(workers) as ex:
        for res in ex.map(one, meta_items):
            rows += res
    return rows


def build(coin, fam, hours, lag):
    w, step = WIN[fam], STEP[fam]
    orc = oracle_series(f"{coin}/usd", hours)
    if orc is None or len(orc.ts) < 1000:
        print(f"  {coin} {fam}: no usable oracle capture on disk")
        return None
    lo, hi = orc.ts[0], orc.ts[-1]
    cand = [t for t in range(int((lo + 3700 + w) // step + 1) * step,
                             int(hi) - step, step) if t >= CHANGE]
    if not cand:
        print(f"  {coin} {fam}: no candidate markets inside oracle window")
        return None
    meta = market_meta([f"{coin}-updown-{fam}-{t}" for t in cand])
    # oracle sigma needs an hour of history before the first market
    osig = rolling_sigma({int(t): p for t, p in zip(orc.ts, orc.px)},
                         lo, hi)
    bser = binance_by_sec(BSYM[coin], lo - 3700, hi + 5)
    if not bser:
        print(f"  {coin} {fam}: binance fetch failed")
        return None
    bsecs = sorted(bser)
    bs = Series(bsecs, [bser[s] for s in bsecs])
    bsig = rolling_sigma(bser, min(bsecs), max(bsecs))

    # HYBRID spot: basis-adjusted Binance, replicating the LIVE bot's
    # state.spot_adj. The bot is a hybrid -- settlement-correct K/S/sigma
    # from the oracle but a sub-second-fresh spot from Binance times the
    # rolling median basis. Pure-oracle spot is up to ~2s stale at the
    # decision instant, so CHAINLINK-z alone is a LAGGED version of what
    # the bot actually computes; HYBRID-z is the faithful replication.
    ob = {int(t): p for t, p in zip(orc.ts, orc.px)}
    common = sorted(set(ob) & set(bser))
    if common:
        basis = pd.Series([ob[s] / bser[s] for s in common],
                          index=pd.Index(common))
        basis = basis.reindex(range(int(lo), int(hi) + 2)).ffill(limit=600)
        basis_med = basis.rolling(600, min_periods=60).median()
    else:
        basis_med = pd.Series(dtype="float64")

    def spot_hybrid(te):
        s = bs.spot(te, max_age=3.0)
        try:
            b = basis_med.loc[int(te)]
        except KeyError:
            return None
        if s is None or not np.isfinite(b):
            return None
        return s * b

    trades = fetch_trades(sorted(meta.items()))
    rows, skipped = [], {"k_orc": 0, "k_bnc": 0}
    kcache = {}
    for slug, ts, p_up, size, is_ask_up, is_ask_dn in trades:
        t0 = int(slug.rsplit("-", 1)[1])
        t1 = t0 + step
        rem = t1 - ts
        if not (2.0 < rem <= w):
            continue
        te = ts - lag
        rem_e = t1 - te
        if slug not in kcache:
            ko_ps, ko_cov, ko_n = orc.integral(t0 - w, t0)
            kb_ps, kb_cov, _ = bs.integral(t0 - w, t0)
            kcache[slug] = (
                ko_ps / w if (ko_cov and ko_n >= 3) else None,
                kb_ps / w if kb_cov else None)
            if kcache[slug][0] is None:
                skipped["k_orc"] += 1
            if kcache[slug][1] is None:
                skipped["k_bnc"] += 1
        K_o, K_b = kcache[slug]

        def z_from(ser, sig, K, spot):
            if K is None or spot is None:
                return None
            sd1 = None
            try:
                sd1 = sig.loc[int(te)]
            except KeyError:
                return None
            if sd1 is None or not np.isfinite(sd1) or sd1 <= 0:
                return None
            S, cov, _ = ser.integral(t1 - w, te)
            if not cov:
                return None
            sigma = sd1 * spot
            sd = sigma * (rem_e ** 3 / 3.0) ** 0.5
            return (S + rem_e * spot - w * K) / sd if sd > 0 else None

        z_o = z_from(orc, osig, K_o, orc.spot(te))
        z_b = z_from(bs, bsig, K_b, bs.spot(te))
        z_h = z_from(orc, osig, K_o, spot_hybrid(te))
        rows.append({"slug": slug, "ts": ts, "rem": rem, "p_up": p_up,
                     "size": size, "is_ask_up": is_ask_up,
                     "is_ask_dn": is_ask_dn, "z_o": z_o, "z_b": z_b,
                     "z_h": z_h, "won": meta[slug]["up_win"]})
    d = pd.DataFrame(rows)
    if len(d):
        n_mkt = d.slug.nunique()
        print(f"  {coin} {fam}: {len(d):,} in-window prints across {n_mkt} "
              f"markets; K unavailable on oracle for {skipped['k_orc']} "
              f"markets, binance {skipped['k_bnc']} (skipped -- report "
              f"survivorship, don't hide it)")
    return d


def run_variant(d, zcol, zmin, max_price, cap, pick):
    q = d[d[zcol].notna()].copy()
    up = q[(q[zcol] >= zmin) & (q.is_ask_up)].copy()
    up["px"] = up.p_up
    up["side_won"] = up.won.astype(float)
    dn = q[(q[zcol] <= -zmin) & (q.is_ask_dn)].copy()
    dn["px"] = 1.0 - dn.p_up
    dn["side_won"] = 1.0 - dn.won.astype(float)
    k = pd.concat([up, dn], ignore_index=True)
    k = k[k.px <= max_price]
    if not len(k):
        return None
    k["pnl_sh"] = k.side_won - k.px - FEE * k.px * (1 - k.px)
    if pick == "uniform":
        tot = k.groupby("slug")["size"].transform("sum")
        k["qty"] = k["size"] * np.minimum(cap / tot.clip(lower=1e-9), 1.0)
    else:
        k = k.sort_values(["slug", "rem"], ascending=[True, False])
        k["cum"] = k.groupby("slug")["size"].cumsum()
        k["qty"] = np.clip(cap - (k.cum - k["size"]), 0, k["size"])
    k = k[k.qty > 0]
    if not len(k):
        return None
    days = max((k.ts.max() - k.ts.min()) / 86400.0, 1e-9)
    tt, ng = clustered_t(k.pnl_sh.values, k.qty.values, k.slug.values)
    return {"sh_day": k.qty.sum() / days,
            "c_sh": 100 * np.average(k.pnl_sh, weights=k.qty),
            "hit": np.average(k.side_won, weights=k.qty),
            "usd_day": (k.qty * k.pnl_sh).sum() / days,
            "t": tt, "n_mkt": ng}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", default="btc,eth,sol,xrp,doge")
    ap.add_argument("--fams", default="5m")
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--lag", type=float, default=1.0)
    ap.add_argument("--zmin", type=float, default=2.0)
    ap.add_argument("--max-price", type=float, default=0.99)
    ap.add_argument("--cap", type=float, default=200.0)
    ap.add_argument("--picks", default="first,uniform")
    a = ap.parse_args()
    print(f"paired tape: BINANCE-z vs CHAINLINK-z, lag {a.lag:g}s, "
          f"|z|>={a.zmin:g}, px<={a.max_price:g}, cap {a.cap:g} sh/mkt\n")
    agg = []
    for coin in a.coins.split(","):
        for fam in a.fams.split(","):
            d = build(coin, fam, a.hours, a.lag)
            if d is None or not len(d):
                continue
            for pick in a.picks.split(","):
                for zcol, name in (("z_b", "BINANCE-z"),
                                   ("z_o", "CHAINLINK-z"),
                                   ("z_h", "HYBRID-z")):
                    r = run_variant(d, zcol, a.zmin, a.max_price,
                                    a.cap, pick)
                    if r is None:
                        print(f"    {name:>12} / {pick:<8} no qualifying "
                              f"prints")
                        continue
                    agg.append({"coin": coin, "fam": fam, "pick": pick,
                                "src": name, **r})
                    print(f"    {name:>12} / {pick:<8} "
                          f"{r['sh_day']:>8,.0f} sh/day  "
                          f"{r['c_sh']:>+6.2f}c/sh  hit {r['hit']:.3f}  "
                          f"${r['usd_day']:>+8,.0f}/day  "
                          f"t={r['t']:+5.2f} (n={r['n_mkt']})")
            print()
    if agg:
        g = pd.DataFrame(agg)
        for pick in g["pick"].unique():
            s = g[g["pick"] == pick]
            piv = s.pivot_table(index=["coin", "fam"], columns="src",
                                values="c_sh")
            if {"BINANCE-z", "CHAINLINK-z"} <= set(piv.columns):
                piv["uplift"] = piv["CHAINLINK-z"] - piv["BINANCE-z"]
            print(f"\nc/share by input series ({pick}):")
            print(piv.to_string(float_format=lambda v: f"{v:+.2f}"))
        print("\nCompare CHAINLINK-z c/share to the live per-coin figures "
              "in src/status.py.\nIf they land close, the live-vs-tape gap "
              "was the Binance proxy and the live\nrun needs no other "
              "explanation.")


if __name__ == "__main__":
    main()
