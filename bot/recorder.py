"""Live recorder: Binance trades, Polymarket RTDS oracle ticks, and CLOB
books for active BTC updown markets.

Writes hourly-rotated jsonl files under data/live/<stream>/YYYY-MM-DD_HH.jsonl
Every record gets local receive time (t_local_us, wall clock) added.

Streams:
- binance: wss://stream.binance.com:9443/ws/btcusdt@trade
- rtds:    wss://ws-live-data.polymarket.com  (crypto_prices chainlink+binance)
- clob:    wss://ws-subscriptions-clob.polymarket.com/ws/market for the
           currently-active and next 15m/5m/1h updown markets (asset ids
           discovered via the Gamma API from deterministic slugs).

Run: python3 bot/recorder.py   (runs until killed)
"""
import asyncio
import json
import os
import time

import aiohttp
import websockets

LIVE_DIR = "data/live"
GAMMA = "https://gamma-api.polymarket.com/markets"


def now_us():
    return int(time.time() * 1_000_000)


class Writer:
    def __init__(self, stream):
        self.stream = stream
        self.f = None
        self.hour = None
        os.makedirs(f"{LIVE_DIR}/{stream}", exist_ok=True)

    def write(self, obj):
        h = time.strftime("%Y-%m-%d_%H", time.gmtime())
        if h != self.hour:
            if self.f:
                self.f.close()
            self.f = open(f"{LIVE_DIR}/{self.stream}/{h}.jsonl", "a")
            self.hour = h
        obj["t_local_us"] = now_us()
        self.f.write(json.dumps(obj, separators=(",", ":")) + "\n")

    def flush(self):
        if self.f:
            self.f.flush()


async def binance_stream():
    w = Writer("binance")
    url = "wss://data-stream.binance.vision/ws/btcusdt@trade"
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                n = 0
                async for msg in ws:
                    d = json.loads(msg)
                    w.write({"p": d.get("p"), "q": d.get("q"),
                             "T": d.get("T"), "m": d.get("m")})
                    n += 1
                    if n % 100 == 0:
                        w.flush()
        except Exception as e:  # noqa: BLE001
            print(f"binance reconnect: {e}", flush=True)
            await asyncio.sleep(2)


async def rtds_stream():
    w = Writer("rtds")
    url = "wss://ws-live-data.polymarket.com"
    sub = {"action": "subscribe", "subscriptions": [
        {"topic": "crypto_prices_chainlink", "type": "*"},
        {"topic": "crypto_prices", "type": "update"},
    ]}
    while True:
        try:
            async with websockets.connect(url) as ws:
                await ws.send(json.dumps(sub))

                async def pinger():
                    while True:
                        await asyncio.sleep(5)
                        await ws.send("PING")

                ptask = asyncio.create_task(pinger())
                n = 0
                try:
                    async for msg in ws:
                        if msg == "PONG":
                            continue
                        try:
                            d = json.loads(msg)
                        except Exception:  # noqa: BLE001
                            continue
                        sym = str(d.get("payload", {}).get("symbol", ""))
                        if not sym.startswith("btc"):
                            continue
                        w.write(d)
                        n += 1
                        if n % 5 == 0:
                            w.flush()
                finally:
                    ptask.cancel()
        except Exception as e:  # noqa: BLE001
            print(f"rtds reconnect: {e}", flush=True)
            await asyncio.sleep(2)


async def discover_assets(session):
    """Asset ids for current & next updown windows (15m, 5m, 1h ET series
    skipped - focus on chainlink families)."""
    out = {}
    now = int(time.time())
    slugs = []
    for step, fam in [(900, "15m"), (300, "5m")]:
        t0 = now - (now % step)
        for k in (0, 1):
            slugs.append(f"btc-updown-{fam}-{t0 + k * step}")
    for slug in slugs:
        try:
            async with session.get(GAMMA, params={"slug": slug},
                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    continue
                arr = await r.json()
                if not arr:
                    continue
                m = arr[0]
                toks = m.get("clobTokenIds")
                if isinstance(toks, str):
                    toks = json.loads(toks)
                if toks:
                    out[slug] = toks[0]  # Up token; tape is mirrored
        except Exception:  # noqa: BLE001
            continue
    return out


async def clob_stream():
    w = Writer("clob")
    url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                assets = await discover_assets(session)
                if not assets:
                    await asyncio.sleep(10)
                    continue
                ids = list(assets.values())
                async with websockets.connect(url, ping_interval=None) as ws:
                    await ws.send(json.dumps(
                        {"assets_ids": ids, "type": "market"}))

                    async def pinger():
                        while True:
                            await asyncio.sleep(10)
                            await ws.send("PING")

                    ptask = asyncio.create_task(pinger())
                    t_end = time.time() + 300  # resubscribe every 5 min
                    try:
                        while time.time() < t_end:
                            msg = await asyncio.wait_for(ws.recv(), timeout=30)
                            if msg == "PONG":
                                continue
                            try:
                                d = json.loads(msg)
                            except Exception:  # noqa: BLE001
                                continue
                            evs = d if isinstance(d, list) else [d]
                            for ev in evs:
                                if isinstance(ev, dict) and "bids" in ev:
                                    ev = dict(ev)
                                    ev["bids"] = ev["bids"][-3:]
                                    ev["asks"] = ev["asks"][-3:]
                                w.write(ev)
                        w.flush()
                    finally:
                        ptask.cancel()
            except Exception as e:  # noqa: BLE001
                print(f"clob reconnect: {e}", flush=True)
                await asyncio.sleep(2)


async def main():
    await asyncio.gather(binance_stream(), rtds_stream(), clob_stream())


if __name__ == "__main__":
    asyncio.run(main())
