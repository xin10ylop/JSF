"""Free Polymarket data acquisition — no Telonex quota, no API key.

Three public, unauthenticated endpoints carry everything the multi-coin
book-efficiency tests need:

  1. gamma-api.polymarket.com/markets?slug=..&closed=true
     Up to 100 slugs per request. Returns conditionId, clobTokenIds,
     outcomePrices (the settled result), volume, liquidity and
     resolutionSource (which is how the 2026-08-07 settlement change is
     datable per coin: 'x-usd' -> 'x-usd-twap-30s-streams').

  2. clob.polymarket.com/prices-history?market=<token>&startTs&endTs&fidelity=1
     ~60s-resolution mid series. Cheap; used for price-at-open.

  3. data-api.polymarket.com/trades?market=<conditionId>&limit=500&offset=
     The actual trade tape: wallet, side, size, price, timestamp.

Sub-commands:
    meta    enumerate windows and pull market metadata + outcome
    prices  pull prices-history for markets in a meta file
    trades  pull the trade tape for markets in a meta file

All three are resumable: output is partitioned by UTC date and existing
partitions are skipped unless --force.
"""
import argparse
import json
import os
import sys
import time
import concurrent.futures as cf
from datetime import datetime, timezone

import pandas as pd
import requests

GAMMA = "https://gamma-api.polymarket.com/markets"
CLOB_PH = "https://clob.polymarket.com/prices-history"
DATA_TRADES = "https://data-api.polymarket.com/trades"
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
STEP = {"5m": 300, "15m": 900}

_local = None


def sess():
    global _local
    import threading
    if _local is None:
        _local = threading.local()
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update(UA)
        a = requests.adapters.HTTPAdapter(pool_connections=64, pool_maxsize=64)
        s.mount("https://", a)
        _local.s = s
    return s


def get(url, params, tries=4):
    for i in range(tries):
        try:
            r = sess().get(url, params=params, timeout=45)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 502, 503, 504):
                time.sleep(1.5 * (i + 1))
                continue
            return None
        except Exception:  # noqa: BLE001
            time.sleep(1.0 * (i + 1))
    return None


def daygrid(start, end):
    d = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    e = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    out = []
    while d <= e:
        out.append(d)
        d = d.fromtimestamp(d.timestamp() + 86400, tz=timezone.utc)
    return out


# ---------------------------------------------------------------- meta
def fetch_meta_chunk(args):
    coin, horizon, t0s = args
    params = [("slug", f"{coin}-updown-{horizon}-{t}") for t in t0s]
    params += [("closed", "true"), ("limit", "500")]
    js = get(GAMMA, params)
    if not js:
        return []
    rows = []
    for m in js:
        try:
            slug = m["slug"]
            t0 = int(slug.rsplit("-", 1)[1])
            op = m.get("outcomePrices")
            op = json.loads(op) if isinstance(op, str) else op
            oc = m.get("outcomes")
            oc = json.loads(oc) if isinstance(oc, str) else oc
            tk = m.get("clobTokenIds")
            tk = json.loads(tk) if isinstance(tk, str) else tk
            if not op or not tk or len(tk) < 2:
                continue
            # index of "Up" outcome, then whether it settled to 1
            iu = 0
            if oc and "Up" in oc:
                iu = oc.index("Up")
            up_win = float(op[iu]) > 0.5
            rows.append({
                "slug": slug, "coin": coin, "horizon": horizon,
                "t0": t0, "t1": t0 + STEP[horizon],
                "cond_id": m.get("conditionId"),
                "tok_up": tk[iu], "tok_dn": tk[1 - iu],
                "up_win": bool(up_win),
                "volume": float(m.get("volume") or 0),
                "liquidity": float(m.get("liquidity") or 0),
                "res_src": m.get("resolutionSource") or "",
                "closed": bool(m.get("closed")),
            })
        except Exception:  # noqa: BLE001
            continue
    return rows


def cmd_meta(a):
    outdir = os.path.join(a.out, "meta")
    os.makedirs(outdir, exist_ok=True)
    jobs = []
    for coin in a.coins:
        for day in daygrid(a.start, a.end):
            ds = day.strftime("%Y-%m-%d")
            fp = os.path.join(outdir, f"{coin}_{a.horizon}_{ds}.parquet")
            if os.path.exists(fp) and not a.force:
                continue
            t00 = int(day.timestamp())
            step = STEP[a.horizon]
            t0s = [t00 + i * step for i in range(86400 // step)]
            chunks = [t0s[i:i + 100] for i in range(0, len(t0s), 100)]
            jobs.append((coin, ds, fp, chunks))
    print(f"meta: {len(jobs)} day-files to fetch", flush=True)

    def one(j):
        coin, ds, fp, chunks = j
        rows = []
        for ch in chunks:
            rows += fetch_meta_chunk((coin, a.horizon, ch))
        if rows:
            df = pd.DataFrame(rows).sort_values("t0")
            df.to_parquet(fp + ".tmp", index=False)
            os.replace(fp + ".tmp", fp)
        return coin, ds, len(rows)

    done = 0
    with cf.ThreadPoolExecutor(a.workers) as ex:
        for coin, ds, n in ex.map(one, jobs):
            done += 1
            if done % 20 == 0 or n == 0:
                print(f"  [{done}/{len(jobs)}] {coin} {ds} n={n}", flush=True)
    print("meta done", flush=True)


# -------------------------------------------------------------- prices
def cmd_prices(a):
    meta = load_meta(a)
    outdir = os.path.join(a.out, "prices")
    os.makedirs(outdir, exist_ok=True)
    meta["day"] = pd.to_datetime(meta["t0"], unit="s", utc=True).dt.strftime("%Y-%m-%d")
    groups = [(c, d, g) for (c, d), g in meta.groupby(["coin", "day"])]
    groups = [(c, d, g) for c, d, g in groups
              if a.force or not os.path.exists(
                  os.path.join(outdir, f"{c}_{a.horizon}_{d}.parquet"))]
    print(f"prices: {len(groups)} day-files, "
          f"{sum(len(g) for _, _, g in groups)} markets", flush=True)

    def one_mkt(r):
        js = get(CLOB_PH, {"market": r.tok_up, "startTs": int(r.t0) - 120,
                           "endTs": int(r.t1) + 60, "fidelity": 1})
        if not js:
            return []
        return [{"slug": r.slug, "t": int(h["t"]), "p": float(h["p"])}
                for h in js.get("history", [])]

    for i, (coin, day, g) in enumerate(groups):
        rows = []
        with cf.ThreadPoolExecutor(a.workers) as ex:
            for res in ex.map(one_mkt, list(g.itertuples())):
                rows += res
        fp = os.path.join(outdir, f"{coin}_{a.horizon}_{day}.parquet")
        pd.DataFrame(rows or [{"slug": "", "t": 0, "p": 0.0}]).to_parquet(
            fp + ".tmp", index=False)
        os.replace(fp + ".tmp", fp)
        print(f"  [{i+1}/{len(groups)}] {coin} {day} pts={len(rows)}", flush=True)
    print("prices done", flush=True)


# -------------------------------------------------------------- trades
def cmd_trades(a):
    meta = load_meta(a)
    outdir = os.path.join(a.out, "trades")
    os.makedirs(outdir, exist_ok=True)
    meta["day"] = pd.to_datetime(meta["t0"], unit="s", utc=True).dt.strftime("%Y-%m-%d")
    groups = [(c, d, g) for (c, d), g in meta.groupby(["coin", "day"])]
    groups = [(c, d, g) for c, d, g in groups
              if a.force or not os.path.exists(
                  os.path.join(outdir, f"{c}_{a.horizon}_{d}.parquet"))]
    print(f"trades: {len(groups)} day-files, "
          f"{sum(len(g) for _, _, g in groups)} markets", flush=True)

    def one_mkt(r):
        out, off = [], 0
        up = str(r.tok_up)
        while True:
            js = get(DATA_TRADES, {"market": r.cond_id, "limit": 500,
                                   "offset": off})
            if not js:
                break
            for x in js:
                if x.get("conditionId") != r.cond_id:
                    continue
                px = float(x["price"])
                is_up = str(x.get("asset")) == up
                out.append({
                    "slug": r.slug, "ts": int(x["timestamp"]),
                    "side": x.get("side"), "size": float(x["size"]),
                    "price": px,
                    # normalise every print to the Up token's price
                    "p_up": px if is_up else 1.0 - px,
                    "is_up_tok": is_up,
                    "wallet": x.get("proxyWallet", "")[:12],
                })
            if len(js) < 500 or off >= a.max_offset:
                break
            off += 500
        return out

    for i, (coin, day, g) in enumerate(groups):
        rows = []
        with cf.ThreadPoolExecutor(a.workers) as ex:
            for res in ex.map(one_mkt, list(g.itertuples())):
                rows += res
        fp = os.path.join(outdir, f"{coin}_{a.horizon}_{day}.parquet")
        if rows:
            pd.DataFrame(rows).to_parquet(fp + ".tmp", index=False)
        else:
            pd.DataFrame({"slug": [], "ts": [], "side": [], "size": [],
                          "price": [], "p_up": [], "is_up_tok": [],
                          "wallet": []}).to_parquet(fp + ".tmp", index=False)
        os.replace(fp + ".tmp", fp)
        print(f"  [{i+1}/{len(groups)}] {coin} {day} trades={len(rows)}",
              flush=True)
    print("trades done", flush=True)


def load_meta(a):
    import glob
    fs = []
    for coin in a.coins:
        fs += sorted(glob.glob(os.path.join(
            a.out, "meta", f"{coin}_{a.horizon}_*.parquet")))
    if not fs:
        sys.exit("no meta files; run `meta` first")
    df = pd.concat([pd.read_parquet(f) for f in fs], ignore_index=True)
    d = pd.to_datetime(df["t0"], unit="s", utc=True)
    m = (d >= a.start) & (d <= a.end + " 23:59:59")
    df = df[m]
    if a.min_volume:
        df = df[df["volume"] >= a.min_volume]
    if getattr(a, "only_slugs", None):
        keep = set(pd.read_parquet(a.only_slugs)["slug"])
        df = df[df["slug"].isin(keep)]
    return df.reset_index(drop=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["meta", "prices", "trades"])
    p.add_argument("--coins", nargs="+",
                   default=["btc", "eth", "sol", "xrp", "doge"])
    p.add_argument("--horizon", default="5m", choices=["5m", "15m"])
    p.add_argument("--start", default="2026-03-01")
    p.add_argument("--end", default="2026-08-09")
    p.add_argument("--out", default="data/pmfree")
    p.add_argument("--workers", type=int, default=20)
    p.add_argument("--min-volume", type=float, default=0.0)
    p.add_argument("--max-offset", type=int, default=2000)
    p.add_argument("--only-slugs", default=None,
                   help="parquet with a 'slug' column; restrict to those")
    p.add_argument("--force", action="store_true")
    a = p.parse_args()
    {"meta": cmd_meta, "prices": cmd_prices, "trades": cmd_trades}[a.cmd](a)


if __name__ == "__main__":
    main()
