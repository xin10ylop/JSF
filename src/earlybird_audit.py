"""Deployed-rule EarlyBird sim over the paginated tape cache.

Entry = first REAL print in (t0+open_delay, t0+45] on ANY side (a print is
proof of a marketable price at that second); pay its side-adjusted price
(mode='ask' adds `tick` on top: assume we cross one tick worse). Filters
mirror bot/strategy.py EarlyBird exactly: |z|>=zmin, fair=Phi(z) side-
adjusted, fair>=price, 0.30<=price<=0.80. Fee 0.07*p*(1-p) on top. Hold
to settlement (gamma outcome). zscale rescales cache z (as-live sigma
variant: live EWMA sigma reads ~1.23x the backtest's 1m-kline sigma, so
the live zmin 0.3 gate ~= cache z 0.37)."""
import json, glob, math, time

CACHE_DIR = "/tmp/claude-0/-home-user-JSF/1e3a1627-7b11-57c3-a9bf-dff6bca1dab2/scratchpad/earlybird"

def load():
    recs, seen = [], set()
    for f in sorted(glob.glob(CACHE_DIR + "/cache_*.jsonl")):
        for l in open(f):
            try: r = json.loads(l)
            except Exception: continue
            k = (r.get("coin"), r.get("t0"))
            if k in seen: continue
            seen.add(k)
            recs.append(r)
    return recs

def Phi(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def sim(recs, coin=None, zmin=0.3, band=(0.30, 0.80), mode="base",
        tick=0.0, zscale=1.0, open_delay=0.0, window=45.0):
    rows = []
    for r in recs:
        if coin and r["coin"] != coin: continue
        z = r["z"] * zscale
        if abs(z) < zmin: continue
        side_up = z > 0
        fair = Phi(z) if side_up else Phi(-z)
        after = [p for p in r["prints"]
                 if r["t0"] + open_delay < p[0] <= r["t0"] + window]
        if not after: continue
        ts, up_px = after[0][0], after[0][1]
        price = up_px if side_up else 1 - up_px
        if mode == "ask": price += tick
        if not (band[0] <= price <= band[1]): continue
        if fair < price: continue
        fee = 0.07 * price * (1 - price)
        won = r["up_won"] if side_up else not r["up_won"]
        rows.append({"t0": r["t0"], "coin": r["coin"], "z": z,
                     "price": price, "won": won,
                     "pnl": (1.0 if won else 0.0) - price - fee})
    return rows

def stats(rows):
    n = len(rows)
    if n == 0: return 0, 0.0, 0.0, 0.0
    m = sum(r["pnl"] for r in rows) / n
    var = sum((r["pnl"] - m) ** 2 for r in rows) / max(1, n - 1)
    t = m / math.sqrt(var / n) if var > 0 else 0.0
    win = 100.0 * sum(r["won"] for r in rows) / n
    return n, 100 * m, t, win

def by_day(rows):
    d = {}
    for r in rows:
        d.setdefault(time.strftime("%m-%d", time.gmtime(r["t0"])), []).append(r["pnl"])
    return d

def main():
    recs = load()
    print(f"records: {len(recs)}  by coin:",
          {c: sum(1 for r in recs if r['coin'] == c)
           for c in {r['coin'] for r in recs}})
    span = lambda rs: (time.strftime('%m-%d %H:%M', time.gmtime(min(r['t0'] for r in rs))),
                       time.strftime('%m-%d %H:%M', time.gmtime(max(r['t0'] for r in rs))))
    for c in sorted({r['coin'] for r in recs}):
        rs = [r for r in recs if r['coin'] == c]
        print(f"  {c}: {span(rs)}")
    for c in sorted({r['coin'] for r in recs}):
        print(f"\n=== {c} deployed-rule grid ===")
        for zmin in (0.3, 0.4, 0.5, 0.6):
            for m, tk, tag in (("base", 0.0, "print "), ("ask", 0.01, "ask+1t")):
                rows = sim(recs, coin=c, zmin=zmin, mode=m, tick=tk)
                n, ev, t, win = stats(rows)
                days = by_day(rows)
                pos = sum(1 for d in days if sum(days[d]) > 0)
                print(f" zmin={zmin} {tag} n={n:>4} EV={ev:>+7.2f}c/sh "
                      f"t={t:>+5.2f} win={win:>3.0f}% days+ {pos}/{len(days)}")

if __name__ == "__main__":
    main()
