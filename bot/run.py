"""Main bot loop (paper mode).

Wires live feeds (Binance ws, RTDS oracle ws, CLOB market ws) into
BotState, evaluates strategies each tick, routes orders to PaperBroker,
and settles positions off the oracle at window end.

Live order mode is intentionally NOT implemented until paper-vs-backtest
reconciliation passes; the execution interface is the single place to swap
PaperBroker for a CLOB client.

Run: python3 bot/run.py            (paper mode, logs to logs/)
"""
import asyncio
import json
import time

import aiohttp
import websockets

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.state import BotState, MarketState, now_us  # noqa: E402
from bot.paper import PaperBroker  # noqa: E402
from bot.risk import Risk  # noqa: E402
from bot.strategy import GzValueMaker, ExtremeTaker, VacuumLadder  # noqa: E402

GAMMA = "https://gamma-api.polymarket.com/markets"
CFG_PATH = os.path.join(os.path.dirname(__file__), "config.json")


def load_cfg():
    with open(CFG_PATH) as f:
        return json.load(f)


class Bot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.state = BotState()
        self.broker = PaperBroker()
        self.risk = Risk(cfg.get("risk", {}))
        self.strategies = []
        if cfg.get("vacuum_ladder", {}).get("enabled", True):
            self.strategies.append(VacuumLadder(cfg.get("vacuum_ladder", {})))
        if cfg.get("gz_maker", {}).get("enabled", False):
            self.strategies.append(GzValueMaker(cfg.get("gz_maker", {})))
        if cfg.get("extreme_taker", {}).get("enabled", False):
            self.strategies.append(ExtremeTaker(cfg.get("extreme_taker", {})))
        self.decisions = open("logs/decisions.jsonl", "a")

    def log_decision(self, obj):
        obj["t_us"] = now_us()
        self.decisions.write(json.dumps(obj, separators=(",", ":")) + "\n")
        self.decisions.flush()

    # ---- market discovery ---------------------------------------------
    async def discover(self, session):
        fams = self.cfg.get("families", {"15m": 900, "5m": 300})
        now = int(time.time())
        for fam, step in fams.items():
            t0 = now - (now % step)
            for k in (0, 1):
                slug = f"btc-updown-{fam}-{t0 + k * step}"
                if slug in self.state.markets:
                    continue
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
                mk = arr[0]
                toks = mk.get("clobTokenIds")
                if isinstance(toks, str):
                    toks = json.loads(toks)
                if not toks:
                    continue
                ws = t0 + k * step
                self.state.markets[slug] = MarketState(
                    slug, toks[0], ws * 1_000_000,
                    (ws + step) * 1_000_000)
                self.log_decision({"kind": "market_added", "slug": slug})
        # settle + drop expired markets (oracle end price known ~2s after t1)
        drop = []
        for slug, m in self.state.markets.items():
            if now_us() > m.t1_us + 10_000_000:
                if m.strike is not None and self.state.oracle_px is not None:
                    # end price: oracle round at/after t1 was captured by
                    # settle watcher below; approximate with last round
                    pass
                drop.append(slug)
        for slug in drop:
            m = self.state.markets.pop(slug)
            result = self._settle_result(m)
            if result is not None:
                pnl = self.broker.settle(slug, result)
                self.risk.on_settle_pnl(pnl)
                self.log_decision({"kind": "settled", "slug": slug,
                                   "result": result, "pnl": pnl})

    def _settle_result(self, m):
        end = getattr(m, "end_px", None)
        if end is None or m.strike is None:
            return None
        return 0 if end >= m.strike else 1

    # ---- feeds ---------------------------------------------------------
    async def binance_feed(self):
        url = "wss://data-stream.binance.vision/ws/btcusdt@trade"
        while True:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    async for msg in ws:
                        d = json.loads(msg)
                        self.state.on_binance(float(d["p"]), int(d["T"]) //
                                              (1000 if d["T"] > 1e14 else 1))
            except Exception as e:  # noqa: BLE001
                self.log_decision({"kind": "feed_err", "feed": "binance",
                                   "err": str(e)[:100]})
                await asyncio.sleep(2)

    async def rtds_feed(self):
        url = "wss://ws-live-data.polymarket.com"
        sub = {"action": "subscribe", "subscriptions": [
            {"topic": "crypto_prices_chainlink", "type": "*"}]}
        while True:
            try:
                async with websockets.connect(url) as ws:
                    await ws.send(json.dumps(sub))

                    async def pinger():
                        while True:
                            await asyncio.sleep(5)
                            await ws.send("PING")
                    pt = asyncio.create_task(pinger())
                    try:
                        async for msg in ws:
                            if msg == "PONG":
                                continue
                            try:
                                d = json.loads(msg)
                            except Exception:  # noqa: BLE001
                                continue
                            pay = d.get("payload", {})
                            if pay.get("symbol") != "btc/usd":
                                continue
                            px = float(pay["value"])
                            ts = int(pay["timestamp"])
                            self.state.on_oracle(px, ts)
                            # capture end price for settling markets
                            for m in self.state.markets.values():
                                if (not hasattr(m, "end_px")
                                        and ts * 1000 >= m.t1_us):
                                    m.end_px = px
                    finally:
                        pt.cancel()
            except Exception as e:  # noqa: BLE001
                self.log_decision({"kind": "feed_err", "feed": "rtds",
                                   "err": str(e)[:100]})
                await asyncio.sleep(2)

    async def clob_feed(self):
        url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
        while True:
            ids = [m.asset_id_up for m in self.state.markets.values()]
            if not ids:
                await asyncio.sleep(5)
                continue
            try:
                async with websockets.connect(url, ping_interval=None) as ws:
                    await ws.send(json.dumps(
                        {"assets_ids": ids, "type": "market"}))

                    async def pinger():
                        while True:
                            await asyncio.sleep(10)
                            await ws.send("PING")
                    pt = asyncio.create_task(pinger())
                    t_end = time.time() + 240
                    try:
                        while time.time() < t_end:
                            msg = await asyncio.wait_for(ws.recv(), timeout=30)
                            if msg == "PONG":
                                continue
                            try:
                                d = json.loads(msg)
                            except Exception:  # noqa: BLE001
                                continue
                            for ev in (d if isinstance(d, list) else [d]):
                                self._on_clob(ev)
                    finally:
                        pt.cancel()
            except Exception as e:  # noqa: BLE001
                self.log_decision({"kind": "feed_err", "feed": "clob",
                                   "err": str(e)[:100]})
                await asyncio.sleep(2)

    def _on_clob(self, ev):
        et = ev.get("event_type")
        aid = ev.get("asset_id")
        if et == "book":
            bids = [(float(x["price"]), float(x["size"]))
                    for x in reversed(ev.get("bids", []))]
            asks = [(float(x["price"]), float(x["size"]))
                    for x in ev.get("asks", [])]
            self.state.on_book(aid, bids, asks, ev.get("timestamp"))
        elif et == "last_trade_price":
            px = float(ev["price"])
            sz = float(ev.get("size", 0))
            self.state.on_trade(aid, px, ev.get("timestamp"))
            self.broker.on_trade_print(aid, px, sz, now_us())

    # ---- decision loop -------------------------------------------------
    async def decide_loop(self):
        while True:
            await asyncio.sleep(1.0)
            st = self.state.staleness()
            for m in list(self.state.markets.values()):
                book_age = (now_us() - m.book_us) / 1e6 if m.book_us else 1e9
                if not self.risk.inputs_ok(st, book_age):
                    continue
                for strat in self.strategies:
                    sig = strat.evaluate(self.state, m, now_us())
                    if not sig:
                        continue
                    pos = self.broker.positions.get((m.slug, sig["side"]),
                                                    {"shares": 0, "cost": 0})
                    nmk = len({k[0] for k, v in
                               self.broker.positions.items()
                               if v["shares"] > 0})
                    px = sig.get("level", sig.get("px", 0.5))
                    size = self.risk.size_ok(pos["shares"], pos["cost"],
                                             sig["size"], px, nmk)
                    if size <= 0:
                        continue
                    self.log_decision({"kind": "signal", "slug": m.slug,
                                       **{k: v for k, v in sig.items()
                                          if k != "action"},
                                       "action": sig["action"],
                                       "sized": size})
                    if sig["action"] == "ladder":
                        for lvl in sig["levels"]:
                            self.broker.maker_buy(
                                m.slug, m.asset_id_up, sig["side"], lvl,
                                size, 200.0,
                                m.t1_us,
                                meta={"reason": sig["reason"]})
                    elif sig["action"] == "maker_buy":
                        self.broker.maker_buy(
                            m.slug, m.asset_id_up, sig["side"], sig["level"],
                            size, sig.get("queue_ahead", 0),
                            now_us() + int(sig.get("ttl_s", 20) * 1e6),
                            meta={"reason": sig["reason"]})
                    elif sig["action"] == "taker_buy":
                        self.broker.taker_buy(
                            m.slug, sig["side"], sig["px"],
                            sig.get("avail", 0), size,
                            meta={"reason": sig["reason"]})

    async def discovery_loop(self):
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    await self.discover(session)
                except Exception as e:  # noqa: BLE001
                    self.log_decision({"kind": "discover_err",
                                       "err": str(e)[:100]})
                await asyncio.sleep(15)

    async def main(self):
        await asyncio.gather(self.binance_feed(), self.rtds_feed(),
                             self.clob_feed(), self.decide_loop(),
                             self.discovery_loop())


if __name__ == "__main__":
    cfg = load_cfg()
    bot = Bot(cfg)
    asyncio.run(bot.main())
