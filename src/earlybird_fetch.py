import json, time, math, urllib.request, argparse

def get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            return json.loads(urllib.request.urlopen(req, timeout=25).read())
        except Exception:
            time.sleep(0.8 + i)
    return None

ap = argparse.ArgumentParser()
ap.add_argument("--coin", required=True)
ap.add_argument("--sym", required=True)          # binance symbol
ap.add_argument("--fam", default="5m")
ap.add_argument("--day-from", type=float, required=True)  # days back, older bound
ap.add_argument("--day-to", type=float, required=True)    # days back, newer bound
ap.add_argument("--out", required=True)
a = ap.parse_args()
step = 300 if a.fam == "5m" else 900
now = int(time.time())
t_hi = now - int(a.day_to * 86400)
t_lo = now - int(a.day_from * 86400)

closes = {}
t = t_lo - 7200
while t < t_hi:
    k1 = get(f"https://data-api.binance.vision/api/v3/klines?symbol={a.sym}&interval=1m&startTime={t*1000}&limit=1000")
    if not k1: break
    for r in k1: closes[int(r[0])//1000] = float(r[4])
    t = int(k1[-1][0])//1000 + 60
    time.sleep(0.08)
print(f"[{a.coin}] 1m candles: {len(closes)}", flush=True)
sig_cache = {}
def sigma1s(t):
    key = t//1800
    if key in sig_cache: return sig_cache[key]
    xs = sorted(u for u in closes if t-3600 <= u < t)
    rs = [math.log(closes[b]/closes[a2]) for a2,b in zip(xs,xs[1:]) if 50<=b-a2<=70]
    v = None
    if len(rs) >= 30:
        mu = sum(rs)/len(rs)
        v = math.sqrt(sum((r-mu)**2 for r in rs)/len(rs)/60.0)
    sig_cache[key] = v
    return v

def all_prints(cond, t0):
    """PAGINATED trades fetch: the API returns newest-first with a 1000-row
    cap per call; busy markets lose their opening prints without paging.
    Page until coverage reaches back past t0-90 or the pages run short."""
    rows = []
    for off in range(0, 15000, 1000):
        arr = get(f"https://data-api.polymarket.com/trades?market={cond}&limit=1000&offset={off}")
        if not arr: break
        rows.extend(arr)
        oldest = min((int(x.get("timestamp") or 0) for x in arr), default=0)
        if oldest > 10**12: oldest //= 1000
        if len(arr) < 1000 or oldest < t0 - 90: break
        time.sleep(0.06)
    return rows

W = 60.0
n_ok = 0
t_first = (t_lo // step) * step
with open(a.out, "w") as out:
    t0 = t_first
    while t0 < t_hi - 2*step:
        t0 += step
        slug = f"{a.coin}-updown-{a.fam}-{t0}"
        mkarr = get(f"https://gamma-api.polymarket.com/markets?slug={slug}&closed=true")
        if not mkarr: continue
        mk = mkarr[0]
        toks = mk.get("clobTokenIds"); toks = json.loads(toks) if isinstance(toks,str) else toks
        op = mk.get("outcomePrices"); op = json.loads(op) if isinstance(op,str) else op
        if not op or not toks: continue
        v = float(op[0])
        if 0.01 < v < 0.99: continue
        ks = get(f"https://data-api.binance.vision/api/v3/klines?symbol={a.sym}&interval=1s&startTime={(t0-62)*1000}&endTime={(t0+1)*1000}&limit=70")
        if not ks: continue
        c1 = {int(r[0])//1000: float(r[4]) for r in ks}
        Kp = [c1[u] for u in range(t0-60, t0) if u in c1]
        if len(Kp) < 40: continue
        K = sum(Kp)/len(Kp)
        spot = c1.get(t0-2) or c1.get(t0-3) or c1.get(t0-1)
        sr = sigma1s(t0)
        if spot is None or not sr: continue
        z = (spot-K)/(sr*spot*math.sqrt((step-W)+W/3))
        mom30 = (spot - c1.get(t0-32, spot))/spot if c1.get(t0-32) else 0.0
        cond = mk.get("conditionId")
        prints = []
        for x in all_prints(cond, t0):
            ts = int(x.get("timestamp") or 0)
            if ts > 10**12: ts //= 1000
            if not (t0-90 <= ts <= t0+step): continue
            p = float(x.get("price", 0)); sz = float(x.get("size", 0))
            if not (0 < p < 1): continue
            oc = str(x.get("outcome") or "").lower()
            asset = str(x.get("asset") or "")
            up_px = p if (asset == str(toks[0]) or oc == "up") else 1-p
            side = str(x.get("side") or "")
            prints.append([ts, round(up_px,4), round(sz,2), side, oc])
        if len(prints) < 2: continue
        out.write(json.dumps({"t0": t0, "coin": a.coin, "up_won": v > 0.5,
                              "z": round(z,4), "mom30": round(mom30*1e4,3),
                              "sigma": round(sr,9),
                              "prints": sorted(prints)}) + "\n")
        n_ok += 1
        if n_ok % 100 == 0: print(f"[{a.coin}] {n_ok} cached", flush=True)
        time.sleep(0.08)
print(f"[{a.coin}] DONE {n_ok} -> {a.out}", flush=True)
