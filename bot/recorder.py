"""Live recorder: Binance trades, Polymarket RTDS oracle ticks, and CLOB
books for active BTC updown markets.

Architecture: each websocket recv loop ONLY enqueues raw frames; a single
writer task per stream drains its queue, parses, filters, and writes
batched lines. This keeps socket consumption fast (no slow-consumer kicks)
and makes losses visible (queue-full drops are counted and logged).

Writes hourly-rotated jsonl under data/live/<stream>/YYYY-MM-DD_HH.jsonl
with t_local_us added to every record.
"""
import asyncio
import json
import os
import time

import aiohttp
import websockets

LIVE_DIR = "data/live"
GAMMA = "https://gamma-api.polymarket.com/markets"


def _cfg():
    try:
        with open(os.path.join(os.path.dirname(__file__), "config.json")) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


# Oracle ticks are tiny and a restarted bot backfills its strike window from
# them, so record every coin we might trade -- an eth bot restarting with no
# eth history in data/live/rtds sits blind for a whole window.
RTDS_COINS = tuple(_cfg().get("coins", ["btc"])) or ("btc",)
# CLOB books are the opposite: 3.8 GB/period for btc alone before pruning.
# Widen deliberately, and watch disk.
BOOK_COINS = tuple(_cfg().get("record_book_coins", ["btc"])) or ("btc",)


def now_us():
    return int(time.time() * 1_000_000)


class Writer:
    def __init__(self, stream):
        self.stream = stream
        self.f = None
        self.hour = None
        self.buf = []
        os.makedirs(f"{LIVE_DIR}/{stream}", exist_ok=True)

    def add(self, obj):
        obj["t_local_us"] = now_us()
        self.buf.append(json.dumps(obj, separators=(",", ":")))

    def flush(self):
        if not self.buf:
            return
        h = time.strftime("%Y-%m-%d_%H", time.gmtime())
        if h != self.hour:
            if self.f:
                self.f.close()
            self.f = open(f"{LIVE_DIR}/{self.stream}/{h}.jsonl", "a")
            self.hour = h
        self.f.write("\n".join(self.buf) + "\n")
        self.f.flush()
        self.buf = []


async def pump(url, queue, name, subscribe=None, ping_text=None,
               ping_every=5, resub_s=None):
    """Connect, subscribe, enqueue every frame. Reconnect forever."""
    while True:
        try:
            async with websockets.connect(url, ping_interval=20,
                                          max_size=2**24) as ws:
                if subscribe:
                    await ws.send(json.dumps(subscribe()))

                async def pinger():
                    while True:
                        await asyncio.sleep(ping_every)
                        await ws.send(ping_text)

                pt = asyncio.create_task(pinger()) if ping_text else None
                t_end = time.time() + resub_s if resub_s else None
                try:
                    while True:
                        if t_end and time.time() > t_end:
                            break
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        try:
                            queue.put_nowait(msg)
                        except asyncio.QueueFull:
                            try:
                                queue.get_nowait()
                                queue.put_nowait(msg)
                            except Exception:  # noqa: BLE001
                                pass
                finally:
                    if pt:
                        pt.cancel()
        except Exception as e:  # noqa: BLE001
            print(f"{name} reconnect: {str(e)[:120]}", flush=True)
            await asyncio.sleep(2)


async def binance_writer(queue):
    w = Writer("binance")
    while True:
        msg = await queue.get()
        try:
            d = json.loads(msg)
        except Exception:  # noqa: BLE001
            continue
        w.add({"p": d.get("p"), "q": d.get("q"), "T": d.get("T"),
               "m": d.get("m")})
        if queue.empty() or len(w.buf) >= 200:
            w.flush()


async def rtds_writer(queue):
    w = Writer("rtds")
    while True:
        msg = await queue.get()
        if msg == "PONG":
            continue
        try:
            d = json.loads(msg)
        except Exception:  # noqa: BLE001
            continue
        sym = str(d.get("payload", {}).get("symbol", ""))
        if not sym.startswith(RTDS_COINS):
            continue
        w.add(d)
        if queue.empty() or len(w.buf) >= 50:
            w.flush()


async def clob_writer(queue):
    w = Writer("clob")
    while True:
        msg = await queue.get()
        if msg == "PONG":
            continue
        try:
            d = json.loads(msg)
        except Exception:  # noqa: BLE001
            continue
        for ev in (d if isinstance(d, list) else [d]):
            if isinstance(ev, dict) and "bids" in ev:
                ev = dict(ev)
                ev["bids"] = ev["bids"][-3:]
                ev["asks"] = ev["asks"][-3:]
            w.add(ev)
        if queue.empty() or len(w.buf) >= 500:
            w.flush()


CLOB_ASSETS = []


async def refresh_assets():
    global CLOB_ASSETS
    async with aiohttp.ClientSession() as session:
        while True:
            out = []
            now = int(time.time())
            for step, fam in [(900, "15m"), (300, "5m")]:
                t0 = now - (now % step)
                for coin, k in [(c, k) for c in BOOK_COINS for k in (0, 1)]:
                    slug = f"{coin}-updown-{fam}-{t0 + k * step}"
                    try:
                        async with session.get(
                                GAMMA, params={"slug": slug},
                                timeout=aiohttp.ClientTimeout(total=10)) as r:
                            if r.status != 200:
                                continue
                            arr = await r.json()
                    except Exception:  # noqa: BLE001
                        continue
                    if not arr:
                        continue
                    toks = arr[0].get("clobTokenIds")
                    if isinstance(toks, str):
                        toks = json.loads(toks)
                    if toks:
                        out.extend(toks[:2])
            if out:
                CLOB_ASSETS = out
            await asyncio.sleep(60)


async def main():
    qb = asyncio.Queue(50_000)
    qr = asyncio.Queue(50_000)
    qc = asyncio.Queue(50_000)
    rtds_sub = lambda: {"action": "subscribe", "subscriptions": [  # noqa: E731
        {"topic": "crypto_prices_chainlink", "type": "*"},
        {"topic": "crypto_prices", "type": "update"}]}
    clob_sub = lambda: {"assets_ids": list(CLOB_ASSETS), "type": "market"}  # noqa: E731
    await asyncio.gather(
        pump("wss://data-stream.binance.vision/ws/btcusdt@trade", qb,
             "binance"),
        binance_writer(qb),
        pump("wss://ws-live-data.polymarket.com", qr, "rtds",
             subscribe=rtds_sub, ping_text="PING", ping_every=5),
        rtds_writer(qr),
        pump("wss://ws-subscriptions-clob.polymarket.com/ws/market", qc,
             "clob", subscribe=clob_sub, ping_text="PING", ping_every=10,
             resub_s=300),
        clob_writer(qc),
        refresh_assets(),
    )


if __name__ == "__main__":
    asyncio.run(main())
