"""Book probe: does a PADDED limit find resting liquidity, live?

Every fill measurement so far used PRINTS -- trades that already
happened. But the venue matches a taker against RESTING orders, so the
honest question is: at the instant our order lands, is there resting
ask size at or below our limit? That is observable directly, with no
money at risk, by watching the real CLOB book.

For each BTC 5m market this records, once per second through the final
90s, the top of each token's ask book. The analyser then replays two
orders at every second:

    unpadded  limit = the ask we saw          (what live sent before)
    padded    limit = ask + pad, value-capped (what live sends now)

and asks whether, ONE ROUND TRIP LATER, resting size existed at or
below each limit. That ratio is the fill rate the venue itself would
have produced -- the number that decides whether the fix works, and
the one thing no tape study and no shadow run can produce.

    python3 src/book_probe.py --minutes 240 --out probe.jsonl
"""
import argparse
import asyncio
import json
import os
import sys
import time

import websockets

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GAMMA = "https://gamma-api.polymarket.com/markets"
WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
TAIL_S = 90.0            # record this much of each market's endgame


def http_get(url):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def discover(coin="btc", fam="5m"):
    """The 5m markets whose endgame is coming up next."""
    step = 300
    now = int(time.time())
    out = []
    for k in (0, 1):
        t0 = (now // step) * step + k * step
        try:
            arr = http_get(f"{GAMMA}?slug={coin}-updown-{fam}-{t0}")
            if not arr:
                continue
            mk = arr[0]
            toks = mk.get("clobTokenIds")
            toks = json.loads(toks) if isinstance(toks, str) else toks
            if toks and len(toks) == 2:
                out.append({"t0": t0, "t1": t0 + step, "up": str(toks[0]),
                            "dn": str(toks[1]),
                            "slug": f"{coin}-updown-{fam}-{t0}"})
        except Exception:  # noqa: BLE001
            continue
    return out


def best_ask(levels):
    """Lowest ask price and its size from a book side."""
    best, sz = None, 0.0
    for lv in levels or []:
        try:
            p = float(lv["price"])
            s = float(lv["size"])
        except Exception:  # noqa: BLE001
            continue
        if s <= 0:
            continue
        if best is None or p < best:
            best, sz = p, s
    return best, sz


async def run(minutes, out_path):
    deadline = time.time() + minutes * 60
    fh = open(out_path, "a")
    seen_done = set()
    n_rows = 0
    while time.time() < deadline:
        mkts = [m for m in discover() if m["slug"] not in seen_done]
        if not mkts:
            await asyncio.sleep(10)
            continue
        ids = [t for m in mkts for t in (m["up"], m["dn"])]
        tok2mkt = {}
        for m in mkts:
            tok2mkt[m["up"]] = (m, "Up")
            tok2mkt[m["dn"]] = (m, "Down")
        books = {}
        t_stop = min(max(m["t1"] for m in mkts) + 5, deadline)
        next_sample = time.time()
        try:
            async with websockets.connect(WS, ping_interval=20) as ws:
                await ws.send(json.dumps({"assets_ids": ids,
                                          "type": "market"}))
                # ONE loop: recv, fold into the book, and sample on the
                # clock. Two cooperating tasks was a silent-failure shape
                # -- an exception in a fire-and-forget sampler is
                # swallowed and the capture writes nothing while the
                # socket looks perfectly healthy.
                while time.time() < t_stop:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=2)
                        if msg != "PONG":
                            d = json.loads(msg)
                            for ev in (d if isinstance(d, list) else [d]):
                                tok = str(ev.get("asset_id") or "")
                                if tok not in tok2mkt:
                                    continue
                                et = ev.get("event_type")
                                if et == "book":
                                    books[tok] = {
                                        float(x["price"]): float(x["size"])
                                        for x in (ev.get("asks") or [])}
                                elif et == "price_change":
                                    cur = books.setdefault(tok, {})
                                    for ch in ev.get("changes") or []:
                                        if str(ch.get("side", "")).upper() \
                                                not in ("SELL", "ASK"):
                                            continue
                                        p = float(ch["price"])
                                        s = float(ch["size"])
                                        if s > 0:
                                            cur[p] = s
                                        else:
                                            cur.pop(p, None)
                    except asyncio.TimeoutError:
                        pass
                    except (json.JSONDecodeError, KeyError, ValueError):
                        continue
                    now = time.time()
                    if now < next_sample:
                        continue
                    next_sample = now + 1.0
                    for m in mkts:
                        rem = m["t1"] - now
                        if not (0 < rem <= TAIL_S):
                            continue
                        row = {"slug": m["slug"], "t": round(now, 2),
                               "rem": round(rem, 2)}
                        for side, tok in (("Up", m["up"]),
                                          ("Down", m["dn"])):
                            bk = books.get(tok) or {}
                            live = [(p, s) for p, s in bk.items() if s > 0]
                            row[side] = sorted(live)[:6]
                        fh.write(json.dumps(row) + "\n")
                        n_rows += 1
                    fh.flush()
        except Exception as e:  # noqa: BLE001
            print("ws error", repr(e)[:120], flush=True)
            await asyncio.sleep(2)
        for m in mkts:
            if time.time() > m["t1"]:
                seen_done.add(m["slug"])
        print(f"{time.strftime('%H:%M:%S')} markets done={len(seen_done)} "
              f"rows={n_rows}", flush=True)
    fh.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=240)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    asyncio.run(run(a.minutes, a.out))


if __name__ == "__main__":
    main()
