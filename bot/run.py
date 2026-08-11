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
        self.pending_settle = {}   # slug -> MarketState awaiting settlement
        self.pending = []          # latency-delayed taker orders
        self.latency_us = int(cfg.get('latency_ms', 150) * 1000)
        self.n_miss = 0            # orders that arrived too late
        self.n_rej = {"binance": 0, "oracle": 0, "book": 0, "killed": 0}
        self.n_clob = 0        # counters surfaced in the health log so a
        self.n_clob_err = 0    # silently-stalled feed is visible at a glance
        self.n_eval_err = 0
        self.n_eval = 0
        self.n_signal = 0
        self.started_us = now_us()   # so a health line can be
                                     # attributed to THIS process

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
                                   "result": result, "pnl": pnl,
                                   "src": "oracle"})
            elif any(k[0] == slug and v["shares"] > 0
                     for k, v in self.broker.positions.items()):
                # We hold a position we cannot yet score. Dropping it here
                # orphans the fill in the broker forever and silently biases
                # the paper record (observed live: a 100-share fill at 0.37
                # that never settled). Park it for the settle loop instead.
                self.pending_settle[slug] = m
                self.log_decision({"kind": "settle_deferred", "slug": slug})

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
        """Spot feed. Same recv-timeout requirement as the oracle feed.

        Without it a quiet or half-dead socket starves the vol estimator and
        `spot_adj`. Observed live: vol sd collapsed to 2.67e-07 (40x too
        low) and 63% of evaluations were rejected for stale inputs, i.e. the
        bot was throwing away most of its opportunities.
        """
        url = "wss://data-stream.binance.vision/ws/btcusdt@trade"
        RECV_TIMEOUT_S = 10
        while True:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    while True:
                        msg = await asyncio.wait_for(
                            ws.recv(), timeout=RECV_TIMEOUT_S)
                        d = json.loads(msg)
                        self.state.on_binance(
                            float(d["p"]),
                            int(d["T"]) // (1000 if d["T"] > 1e14 else 1))
            except asyncio.TimeoutError:
                self.log_decision({"kind": "binance_silent",
                                   "note": f"no trade in {RECV_TIMEOUT_S}s"})
            except Exception as e:  # noqa: BLE001
                self.log_decision({"kind": "feed_err", "feed": "binance",
                                   "err": str(e)[:100]})
                await asyncio.sleep(2)

    async def rtds_feed(self):
        """Chainlink oracle feed.

        MUST have a receive timeout. `async for msg in ws` blocks forever if
        the server stops sending without closing the socket -- no exception,
        no reconnect, and the bot runs on a frozen oracle. Observed live: the
        newest round went 601s stale while the process looked perfectly
        healthy, and the stale strike against a live spot manufactured fake
        signals that cost -$162 of paper P&L in one bucket.

        Three defences: a recv timeout, a periodic forced reconnect, and a
        watchdog on the age of the newest ROUND (not merely of receipt --
        the feed can keep sending heartbeats while the round is stuck).
        """
        url = "wss://ws-live-data.polymarket.com"
        sub = {"action": "subscribe", "subscriptions": [
            {"topic": "crypto_prices_chainlink", "type": "*"}]}
        RECV_TIMEOUT_S = 15
        CYCLE_S = 600
        MAX_ROUND_AGE_S = 60
        while True:
            try:
                async with websockets.connect(url, ping_interval=None) as ws:
                    await ws.send(json.dumps(sub))

                    async def pinger():
                        while True:
                            await asyncio.sleep(5)
                            await ws.send("PING")
                    pt = asyncio.create_task(pinger())
                    t_end = time.time() + CYCLE_S
                    try:
                        while time.time() < t_end:
                            msg = await asyncio.wait_for(
                                ws.recv(), timeout=RECV_TIMEOUT_S)
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
                            for m in self.state.markets.values():
                                if (m.end_px is None
                                        and ts * 1000 >= m.t1_us):
                                    m.end_px = px
                            # the socket is live but the ROUND may be stuck
                            if self.state.oracle_age_s() > MAX_ROUND_AGE_S:
                                self.log_decision({
                                    "kind": "rtds_round_stale",
                                    "age_s": round(
                                        self.state.oracle_age_s(), 1)})
                                break
                    finally:
                        pt.cancel()
            except asyncio.TimeoutError:
                self.log_decision({"kind": "rtds_silent",
                                   "note": f"no msg in {RECV_TIMEOUT_S}s, "
                                           "reconnecting"})
            except Exception as e:  # noqa: BLE001
                self.log_decision({"kind": "feed_err", "feed": "rtds",
                                   "err": str(e)[:100]})
                await asyncio.sleep(2)

    async def clob_feed(self):
        url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
        queue = asyncio.Queue(50_000)

        async def consumer():
            """Drain the queue. MUST NOT die.

            This is a fire-and-forget task, so an exception here is not
            propagated: the process stays alive, systemd sees it running,
            the queue silently fills, and no book update is ever processed
            again. Book age then grows past the staleness limit so the 1s
            fallback stops trading too -- fills simply stop with nothing
            appearing to be wrong. Every event is therefore isolated, and
            the task is restarted if it ever exits.
            """
            while True:
                msg = await queue.get()
                if msg == "PONG":
                    continue
                try:
                    d = json.loads(msg)
                except Exception:  # noqa: BLE001
                    continue
                for ev in (d if isinstance(d, list) else [d]):
                    try:
                        self._on_clob(ev)
                        self.n_clob += 1
                    except Exception as e:  # noqa: BLE001
                        self.n_clob_err += 1
                        if self.n_clob_err <= 20:
                            self.log_decision({"kind": "clob_handler_err",
                                               "err": repr(e)[:200]})

        async def supervise_consumer():
            while True:
                try:
                    await consumer()
                except Exception as e:  # noqa: BLE001
                    self.log_decision({"kind": "consumer_died",
                                       "err": repr(e)[:200]})
                await asyncio.sleep(1)

        asyncio.create_task(supervise_consumer())
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
            # Full snapshot: replace the level map for this token.
            # Polymarket orders levels worst-to-best; sort explicitly.
            bids = sorted(((float(x["price"]), float(x["size"]))
                           for x in ev.get("bids", [])), key=lambda t: -t[0])
            asks = sorted(((float(x["price"]), float(x["size"]))
                           for x in ev.get("asks", [])), key=lambda t: t[0])
            m = self.state.on_book(aid, bids, asks, ev.get("timestamp"))
            self._maybe_eval(m)
        elif et == "price_change":
            # 97.2% of CLOB messages. Ignoring these leaves the book stale
            # between the 1.9% that are snapshots -- and the dips this
            # strategy trades arrive as deltas, so the bot never saw them.
            for ch in ev.get("price_changes", []) or []:
                try:
                    m = self.state.on_price_change(
                        str(ch.get("asset_id")), float(ch["price"]),
                        float(ch["size"]), ch.get("side"))
                except (TypeError, ValueError, KeyError):
                    continue
                self._maybe_eval(m)
        elif et == "last_trade_price":
            px = float(ev["price"])
            sz = float(ev.get("size", 0))
            if mirror:
                px = 1.0 - px
            self.state.on_trade(up_aid, px, ev.get("timestamp"))
            self.broker.on_trade_print(up_aid, px, sz, now_us())

    # ---- decision loop -------------------------------------------------
    async def settle_loop(self):
        """Settle deferred markets: retry the oracle, then fall back to the
        venue's own resolved outcome.

        Gamma is authoritative and free, so there is no reason for a paper
        fill to go unscored. Anything still unresolved after `give_up_s` is
        logged loudly rather than dropped.
        """
        give_up_s = 3600
        async with aiohttp.ClientSession() as session:
            while True:
                await asyncio.sleep(30)
                for slug, m in list(self.pending_settle.items()):
                    result = self._settle_result(m)
                    src = "oracle"
                    if result is None and now_us() > m.t1_us + 120_000_000:
                        result = await self._gamma_result(session, slug)
                        src = "gamma"
                    if result is not None:
                        pnl = self.broker.settle(slug, result)
                        self.risk.on_settle_pnl(pnl)
                        self.log_decision({"kind": "settled", "slug": slug,
                                           "result": result, "pnl": pnl,
                                           "src": src})
                        self.pending_settle.pop(slug, None)
                    elif now_us() > m.t1_us + give_up_s * 1_000_000:
                        self.log_decision({"kind": "settle_FAILED",
                                           "slug": slug,
                                           "note": "position left unscored"})
                        self.pending_settle.pop(slug, None)

    async def _gamma_result(self, session, slug):
        """0 if Up won, 1 if Down won, from the venue's settled outcome."""
        try:
            async with session.get(
                    GAMMA, params={"slug": slug, "closed": "true"},
                    timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    return None
                arr = await r.json()
        except Exception:  # noqa: BLE001
            return None
        if not arr:
            return None
        mk = arr[0]
        op = mk.get("outcomePrices")
        oc = mk.get("outcomes")
        if isinstance(op, str):
            op = json.loads(op)
        if isinstance(oc, str):
            oc = json.loads(oc)
        if not op:
            return None
        iu = oc.index("Up") if oc and "Up" in oc else 0
        try:
            return 0 if float(op[iu]) > 0.5 else 1
        except (TypeError, ValueError):
            return None

    async def health_loop(self):
        """Periodic snapshot of every input the pricer needs.

        Emits quickly after start, then every 30s. Waiting a full 30s first
        means `status.py` run right after a restart reads the PREVIOUS
        process's line and attributes it to the new build.

        Without this a silent bot is indistinguishable from a bot with no
        signals: fair() returns None if any of oracle history, basis or vol
        is missing, and each has a different fix.
        """
        first = True
        while True:
            await asyncio.sleep(5 if first else 30)
            first = False
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
                "uptime_s": round((now_us() - self.started_us) / 1e6, 1),
                "pending_settle": len(self.pending_settle),
                "pending_orders": len(self.pending), "misses": self.n_miss,
                "rejects": dict(self.n_rej),
                "clob_evs": self.n_clob, "clob_errs": self.n_clob_err,
                "evals": self.n_eval, "eval_errs": self.n_eval_err,
                "signals": self.n_signal, "killed": self.risk.killed,
                "funnel": next((st.f for st in self.strategies
                                if hasattr(st, "f")), None),
                "day_pnl": round(self.risk.day_pnl, 2),
                "oracle_hist": len(s.oracle_hist),
                "oracle_rate": s.oracle_rate(), "basis_n": len(s.basis),
                "vol_var": s.vol.var, "binance_px": s.binance_px,
                "oracle_px": s.oracle_px, "spot_adj": s.spot_adj(),
                "stale": {k: round(v, 1) for k, v in s.staleness().items()},
                "detail": mk})

    def _process_pending(self):
        """Execute delayed orders against the book as it is NOW."""
        if not self.pending:
            return
        now = now_us()
        keep = []
        for o in self.pending:
            if now < o["fire_us"]:
                keep.append(o)
                continue
            m = self.state.markets.get(o["slug"])
            if m is None:
                self.n_miss += 1
                continue
            if o["side"] == "Up":
                px, sz = m.best_ask()
            else:
                px, sz, _src = m.best_ask_dn()
            if px is None or px > o["limit"] + 1e-9 or sz <= 0:
                self.n_miss += 1          # the ask we aimed at is gone
                self.log_decision({"kind": "taker_miss", "slug": o["slug"],
                                   "side": o["side"], "limit": o["limit"],
                                   "now_ask": px})
                continue
            self.broker.taker_buy(o["slug"], o["side"], px, sz, o["size"],
                                  meta=o["meta"])
        self.pending = keep

    def _maybe_eval(self, m):
        """Evaluate immediately if this market is inside its settle window."""
        if m is None:
            return
        rem = (m.t1_us - now_us()) / 1e6
        self._process_pending()
        if 0 < rem <= m.w:
            self._try_market(m)

    def _try_market(self, m):
        """Guarded wrapper: one market's failure must never stop the loop."""
        try:
            self._try_market_inner(m)
        except Exception as e:  # noqa: BLE001
            self.n_eval_err += 1
            if self.n_eval_err <= 20:
                self.log_decision({"kind": "eval_err", "slug": m.slug,
                                   "err": repr(e)[:200]})

    def _try_market_inner(self, m):
        """Evaluate every strategy for one market and route any signal.

        Shared by the 1s safety-net loop and the event-driven book handler,
        so both paths take exactly the same decision.
        """
        self.n_eval += 1
        st = self.state.staleness()
        book_age = (now_us() - m.book_us) / 1e6 if m.book_us else 1e9
        if not self.risk.inputs_ok(st, book_age):
            # Attribute the rejection. A market that is not currently in its
            # window legitimately has a stale book; a stale FEED is a fault.
            if self.risk.killed:
                self.n_rej["killed"] += 1
            elif st["binance_s"] >= self.risk.stale_binance_s:
                self.n_rej["binance"] += 1
            elif st["oracle_s"] >= self.risk.stale_oracle_s:
                self.n_rej["oracle"] += 1
            else:
                self.n_rej["book"] += 1
            return
        for strat in self.strategies:
            sig = strat.evaluate(self.state, m, now_us())
            if not sig:
                continue
            # Never hold BOTH sides of the same market. z can flip sign
            # late in the window (as rem -> 0 the margin can cross zero),
            # and the per-market caps are keyed by (slug, side), so nothing
            # else would stop the bot buying Up at ~0.9 and then Down at
            # ~0.9 -- paying ~1.90 for a guaranteed 1.00 payoff.
            other = "Down" if sig["side"] == "Up" else "Up"
            if self.broker.positions.get((m.slug, other), {}).get("shares", 0) > 0:
                self.log_decision({"kind": "blocked_opposite_side",
                                   "slug": m.slug, "side": sig["side"],
                                   "holding": other})
                continue
            pos = self.broker.positions.get((m.slug, sig["side"]),
                                            {"shares": 0, "cost": 0})
            # Orders queued for latency are REAL exposure the moment they
            # are sent. Sizing against the broker position alone lets every
            # order queued inside the latency window see an empty book and
            # get approved -- observed live as 700 shares in a market whose
            # cap is ~187, from seven identical 39.3-share fills at 0.800.
            q_sh = sum(o["size"] for o in self.pending
                       if o["slug"] == m.slug and o["side"] == sig["side"])
            q_usd = sum(o["size"] * o["limit"] for o in self.pending
                        if o["slug"] == m.slug and o["side"] == sig["side"])
            pos = {"shares": pos["shares"] + q_sh,
                   "cost": pos["cost"] + q_usd}
            nmk = len({k[0] for k, v in self.broker.positions.items()
                       if v["shares"] > 0} | {o["slug"] for o in self.pending})
            px = sig.get("level", sig.get("px", 0.5))
            size = self.risk.size_ok(pos["shares"], pos["cost"],
                                     sig["size"], px, nmk)
            if size <= 0:
                continue
            self.n_signal += 1
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
                # Do NOT fill at the price we just saw. A marketable order
                # reaches the venue `latency_ms` later and executes against
                # whatever is resting THEN, up to our limit. 63% of paper
                # P&L came from sub-0.85 dips, which are exactly the prices
                # that disappear fastest -- filling them instantly is the
                # single biggest way paper flatters reality.
                self.pending.append({
                    "slug": m.slug, "side": sig["side"],
                    "limit": sig["px"], "size": size,
                    "fire_us": now_us() + self.latency_us,
                    "meta": {"reason": sig["reason"],
                             "oracle_age_s": sig.get("oracle_age_s"),
                             "z": sig.get("z"),
                             "ev_est": sig.get("ev_est"),
                             "seen_px": sig["px"]}})

    async def decide_loop(self):
        """1s safety net. The primary path is event-driven off book updates
        (see _on_clob); this catches markets whose book has gone quiet."""
        while True:
            await asyncio.sleep(1.0)
            self._process_pending()
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
                             self.discovery_loop(), self.health_loop(),
                             self.settle_loop())


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
