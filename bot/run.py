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
from bot.strategy import (GzValueMaker, ExtremeTaker, VacuumLadder,  # noqa: E402
                          RollAvgEdge)

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
        if cfg.get("rollavg_edge", {}).get("enabled", True):
            self.strategies.append(RollAvgEdge(cfg.get("rollavg_edge", {})))
        if cfg.get("vacuum_ladder", {}).get("enabled", False):
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
        # single coin by design: BotState carries one oracle ring buffer,
        # one vol estimator and one basis series. BTC alone supplies 2.42M
        # of the 2.98M qualifying shares measured post-change, so the
        # multi-coin build (per-coin state) is a capacity upgrade, not a
        # prerequisite. See reports/findings.md.
        coin = self.cfg.get("coin", "btc")
        now = int(time.time())
        for fam, step in fams.items():
            t0 = now - (now % step)
            for k in (0, 1):
                slug = f"{coin}-updown-{fam}-{t0 + k * step}"
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
                    (ws + step) * 1_000_000,
                    asset_id_dn=toks[1] if len(toks) > 1 else None)
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
        """Settle on the VERIFIED post-2026-08-07 contract:

            Up iff mean(P over [t1-w, t1)) >= mean(P over [t0-w, t0))

        Both averages trail. Settling paper fills on the pre-change rule
        (end price vs strike price) would silently mis-score every trade,
        so this reads both averages straight off the oracle ring buffer.
        """
        K = self.state.strike_avg(m)
        r_ps, r_secs = self.state.settle_sum_so_far(m, m.t1_us)
        if K is None or r_secs < m.w * 0.9:
            return None
        return 0 if (r_ps / r_secs) >= K else 1

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
                                if (m.end_px is None
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
        queue = asyncio.Queue(50_000)

        async def consumer():
            while True:
                msg = await queue.get()
                if msg == "PONG":
                    continue
                try:
                    d = json.loads(msg)
                except Exception:  # noqa: BLE001
                    continue
                for ev in (d if isinstance(d, list) else [d]):
                    self._on_clob(ev)

        asyncio.create_task(consumer())
        while True:
            ids = []
            for m in self.state.markets.values():
                ids.append(m.asset_id_up)
                if m.asset_id_dn:
                    ids.append(m.asset_id_dn)
            if not ids:
                await asyncio.sleep(5)
                continue
            try:
                async with websockets.connect(url, ping_interval=None, max_size=2**24) as ws:
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
                            try:
                                queue.put_nowait(msg)
                            except asyncio.QueueFull:
                                pass
                    finally:
                        pt.cancel()
            except Exception as e:  # noqa: BLE001
                self.log_decision({"kind": "feed_err", "feed": "clob",
                                   "err": str(e)[:100]})
                await asyncio.sleep(2)

    def _on_clob(self, ev):
        et = ev.get("event_type")
        aid = ev.get("asset_id")
        # map down-token events to the up-token view (mirrored semantics,
        # matching the research tape: down trade at p == up trade at 1-p)
        up_aid = aid
        mirror = False
        for m in self.state.markets.values():
            if m.asset_id_dn == aid:
                up_aid = m.asset_id_up
                mirror = True
                break
        if et == "book":
            if mirror:
                return  # book state tracked on the up token only
            # Polymarket orders levels worst-to-best; sort explicitly rather
            # than relying on reversed()/as-sent order.
            bids = sorted(((float(x["price"]), float(x["size"]))
                           for x in ev.get("bids", [])), key=lambda t: -t[0])
            asks = sorted(((float(x["price"]), float(x["size"]))
                           for x in ev.get("asks", [])), key=lambda t: t[0])
            self.state.on_book(aid, bids, asks, ev.get("timestamp"))
            # Evaluate IMMEDIATELY on a book change inside the settle window.
            # The favoured side rests at ~0.99 most of the time and dips to
            # 0.87-0.92 only in the instants around a trade; a 1s polling
            # loop walks past most of those. Measured take rate on recorded
            # books with 1s polling was 1 market in 17.
            for m in self.state.markets.values():
                if m.asset_id_up != aid:
                    continue
                rem = (m.t1_us - now_us()) / 1e6
                if 0 < rem <= m.w:
                    self._try_market(m)
                break
        elif et == "last_trade_price":
            px = float(ev["price"])
            sz = float(ev.get("size", 0))
            if mirror:
                px = 1.0 - px
            self.state.on_trade(up_aid, px, ev.get("timestamp"))
            self.broker.on_trade_print(up_aid, px, sz, now_us())

    # ---- decision loop -------------------------------------------------
    async def health_loop(self):
        """Periodic snapshot of every input the pricer needs.

        Without this a silent bot is indistinguishable from a bot with no
        signals: fair() returns None if any of oracle history, basis or vol
        is missing, and each has a different fix.
        """
        while True:
            await asyncio.sleep(30)
            s = self.state
            mk = {}
            for slug, m in list(s.markets.items())[:6]:
                mk[slug] = {
                    "rem_s": round((m.t1_us - now_us()) / 1e6, 1),
                    "K": (round(s.strike_avg(m), 2)
                          if s.strike_avg(m) else None),
                    "fair": (round(s.fair(m), 4)
                             if s.fair(m) is not None else None),
                    "z": (round(s.zscore(m), 2)
                          if s.zscore(m) is not None else None),
                    "bid": m.best_bid()[0], "ask": m.best_ask()[0],
                    "book_age_s": (round((now_us() - m.book_us) / 1e6, 1)
                                   if m.book_us else None)}
            self.log_decision({
                "kind": "health", "markets": len(s.markets),
                "oracle_hist": len(s.oracle_hist), "basis_n": len(s.basis),
                "vol_var": s.vol.var, "binance_px": s.binance_px,
                "oracle_px": s.oracle_px, "spot_adj": s.spot_adj(),
                "stale": {k: round(v, 1) for k, v in s.staleness().items()},
                "detail": mk})

    def _try_market(self, m):
        """Evaluate every strategy for one market and route any signal.

        Shared by the 1s safety-net loop and the event-driven book handler,
        so both paths take exactly the same decision.
        """
        st = self.state.staleness()
        book_age = (now_us() - m.book_us) / 1e6 if m.book_us else 1e9
        if not self.risk.inputs_ok(st, book_age):
            return
        for strat in self.strategies:
            sig = strat.evaluate(self.state, m, now_us())
            if not sig:
                continue
            pos = self.broker.positions.get((m.slug, sig["side"]),
                                            {"shares": 0, "cost": 0})
            nmk = len({k[0] for k, v in self.broker.positions.items()
                       if v["shares"] > 0})
            px = sig.get("level", sig.get("px", 0.5))
            size = self.risk.size_ok(pos["shares"], pos["cost"],
                                     sig["size"], px, nmk)
            if size <= 0:
                continue
            self.log_decision({"kind": "signal", "slug": m.slug,
                               **{k: v for k, v in sig.items()
                                  if k != "action"},
                               "action": sig["action"], "sized": size})
            if sig["action"] == "ladder":
                for lvl in sig["levels"]:
                    self.broker.maker_buy(m.slug, m.asset_id_up, sig["side"],
                                          lvl, size, 200.0, m.t1_us,
                                          meta={"reason": sig["reason"]})
            elif sig["action"] == "maker_buy":
                self.broker.maker_buy(
                    m.slug, m.asset_id_up, sig["side"], sig["level"], size,
                    sig.get("queue_ahead", 0),
                    now_us() + int(sig.get("ttl_s", 20) * 1e6),
                    meta={"reason": sig["reason"]})
            elif sig["action"] == "taker_buy":
                self.broker.taker_buy(m.slug, sig["side"], sig["px"],
                                      sig.get("avail", 0), size,
                                      meta={"reason": sig["reason"]})

    async def decide_loop(self):
        """1s safety net. The primary path is event-driven off book updates
        (see _on_clob); this catches markets whose book has gone quiet."""
        while True:
            await asyncio.sleep(1.0)
            for m in list(self.state.markets.values()):
                self._try_market(m)

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
                             self.discovery_loop(), self.health_loop())


def acquire_lock(path="logs/bot.lock"):
    """Refuse to start a second instance.

    Two bots sharing logs/paper_fills.jsonl double-count every fill and
    silently corrupt the paper record that the whole go/no-go decision
    rests on. An advisory flock is enough: it is released automatically if
    the process dies, so restarts stay clean.
    """
    import fcntl
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another bot/run.py already holds logs/bot.lock — exiting",
              file=sys.stderr)
        raise SystemExit(3)
    fh.write(str(os.getpid()))
    fh.flush()
    return fh                       # keep the handle alive for the process


if __name__ == "__main__":
    _lock = acquire_lock()
    cfg = load_cfg()
    bot = Bot(cfg)
    asyncio.run(bot.main())
