"""Main bot loop: one codebase, three execution modes.

Wires live feeds (Binance ws, RTDS oracle ws, CLOB market ws) into
BotState, evaluates strategies each tick, and settles positions off the
oracle at window end. `mode` in the config picks what a taker signal
becomes -- everything upstream of that branch is IDENTICAL, which is the
whole point: the live record and the paper record must disagree only
about execution, never about signals.

  paper   simulate fills through the latency/participation model
          (PaperBroker), logs to logs/<coin>/
  shadow  paper simulation PLUS a signed-order log of exactly what live
          mode would have sent, logs to logs/live/<coin>/
  live    send a real FAK to the CLOB via bot/live.py and book the
          venue's actual fill into the same broker/risk/settle path.
          No simulated latency: the venue's own 250ms hold and our real
          rtt replace the model. Logs to logs/live/<coin>/

Run: python3 bot/run.py --coin btc                      (paper)
     python3 bot/run.py --coin btc --cfg bot/config.live.json
"""
import asyncio
import json
import random
import threading
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
                          RollAvgEdge, ZMaker, EarlyBird,
                          JumpScalp)

GAMMA = "https://gamma-api.polymarket.com/markets"
CFG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
_LOG_LOCK = threading.Lock()   # decision-log writes come from several
                               # threads; one bot per process (flock)


LOCAL_CFG_PATH = os.path.join(os.path.dirname(__file__), "config.local.json")


def load_cfg(path=None):
    """config.json (or `path`), then bot/config.local.json shallow-merged
    over it.

    Host-specific values (rtt_ms above all) belong to the machine, not the
    repo. Writing them into the tracked config made `git pull` abort with
    "your local changes would be overwritten" -- which is exactly how the
    multi-coin build silently failed to deploy. The local file is
    gitignored, so measuring latency and pulling code never collide again.
    """
    with open(path or CFG_PATH) as f:
        cfg = json.load(f)
    if os.path.exists(LOCAL_CFG_PATH):
        with open(LOCAL_CFG_PATH) as f:
            cfg.update(json.load(f))
    return cfg


class Bot:
    def __init__(self, cfg, coin=None):
        self.cfg = cfg
        self.coin = coin or cfg.get("coin", "btc")
        self.mode = cfg.get("mode", "paper")
        if self.mode not in ("paper", "shadow", "live"):
            raise SystemExit(f"unknown mode {self.mode!r}")
        self.state = BotState(self.coin)
        # One process per coin, so the record has to be per coin too: a
        # shared fill log would interleave five bots' fills and make the
        # per-coin edge unreadable. src/score_paper.py globs logs/*/.
        # Shadow/live instances live under logs/live/<coin> -- OUTSIDE
        # that glob -- so the frozen paper baseline the divergence report
        # compares against is never contaminated by micro-sized records.
        self.logdir = (os.path.join("logs", "live", self.coin)
                       if self.mode != "paper"
                       else os.path.join("logs", self.coin))
        os.makedirs(self.logdir, exist_ok=True)
        self.broker = PaperBroker(
            log_path=os.path.join(self.logdir, "paper_fills.jsonl"))
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
        eb = cfg.get("earlybird", {})
        self._open_eval_s = 0.0
        if eb.get("enabled", False) and self.coin in eb.get(
                "coins", ["btc"]):
            # coin-gated: the measured edge is btc-concentrated
            # (+14.3c t=3.07 vs eth +2.7 t=1.1, sol +1.2 t=0.5)
            self.strategies.append(EarlyBird(eb))
            # The event-driven eval path only covers the settle window;
            # without this, EarlyBird sees the opening book at 1 Hz only
            # (the safety-net loop) while its measured entry is the FIRST
            # print after open. +2s slack so the last in-window book tick
            # still evaluates; EarlyBird itself enforces the 45s gate.
            self._open_eval_s = float(eb.get("open_window_s", 45.0)) + 2.0
        # The maker leg runs as a MEASUREMENT: signals are simulated
        # against real trade prints in a fully ISOLATED ledger --
        # separate fills file, separate settle lines, never touching the
        # real-money broker, risk state, or the daily stop. Live taker
        # orders and the maker sim measure two monetization paths of the
        # same edge side by side; the divergence report compares them.
        self.maker_broker = None
        if cfg.get("zmaker", {}).get("enabled", False):
            self.strategies.append(ZMaker(cfg.get("zmaker", {})))
        js = cfg.get("jumpscalp", {})
        if js.get("enabled", False) and self.coin in js.get("coins", ["btc"]):
            # btc only until the eth/sol out-of-sample blank is explained
            self.strategies.append(JumpScalp(js))
            self._scalps = {}      # (slug, side) -> open scalp position
            self.maker_broker = PaperBroker(
                log_path=os.path.join(self.logdir, "maker_fills.jsonl"))
        self.decisions = open(
            os.path.join(self.logdir, "decisions.jsonl"), "a")
        self.pending_settle = {}   # slug -> MarketState awaiting settlement
        self.pending = []          # latency-delayed taker orders (paper sim)
        self.inflight = []         # live orders at the venue, result pending
        self.executor = None
        if self.mode != "paper":
            from bot.live import LiveExecutor
            self.executor = LiveExecutor(
                shadow=(self.mode != "live"),
                log_path=os.path.join(self.logdir, "orders.jsonl"))
            if self.mode == "live":
                # Fail-fast: derive credentials NOW, before any feed
                # starts. A live bot that silently cannot authenticate
                # would run the whole strategy and drop every order.
                cl = self.executor.client
                if cl.get_closed_only_mode():
                    raise SystemExit("account is in closed-only mode; "
                                     "refusing to start live")
                # ALL order submissions ride ONE worker thread. The SDK's
                # httpx transport multiplexes HTTP/2 streams over a single
                # connection, and concurrent submissions from pool threads
                # corrupted its stream table on launch day -- KeyError(23)
                # and KeyError(25), consecutive client stream ids, two
                # orders lost. Serialized orders cannot collide; ops
                # traffic gets its own client in _live_ops_pass.
                from concurrent.futures import ThreadPoolExecutor
                self._order_pool = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="order")
                self._ops_ex = None
                # (slug,side) -> until_us. Set when an order's outcome is
                # NOT terminal (venue 'delayed'/'live', timeouts): the
                # venue reserves collateral and may still execute it, so
                # re-firing there is the double-fill path.
                self._refire_block = {}
                self._backoff_until = 0     # venue 429/post-only pause
                self._prewarmed = {}   # slug -> warmed-at us (re-warm
                                       # after the ~10min SDK cache TTL)
                self._settle_verify = []    # (slug, our_result, due_us)
                self._day_bal_anchor = None  # (utc_day, balance at start)
                self._booked_oids = set()   # order_ids whose venue trades
                                            # are booked (or being booked)
        self._loop = None                    # set in main()
        # Total delay before a taker order can match. `venue_hold_ms` is the
        # venue's own documented 250 ms hold on crypto up/down markets ("the
        # order is held for 250 ms, then validation runs again and the order
        # is matched or placed on the book"); `rtt_ms` is OUR round trip,
        # which src/probe_latency.py measures rather than guesses.
        self.latency_us = int((cfg.get("venue_hold_ms", 250)
                               + cfg.get("rtt_ms", 160)) * 1000)
        self.fill_cfg = {"participation": 0.5, "vol_participation": 0.25,
                         "vol_window_s": 5.0, "reject_rate": 0.01,
                         "min_order_size": 5.0, "use_tape_cap": True,
                         **cfg.get("fill", {})}
        self.n_sent = 0            # taker orders actually queued
        self.n_miss = 0            # orders that arrived too late
        # WHY they missed. "the ask moved" and "our own tape cap fell below
        # the venue's 5-share minimum" are both misses and need opposite
        # fixes, so a bare count cannot be acted on.
        self.n_miss_why = {"ask_gone": 0, "too_small": 0, "no_market": 0}
        self.n_reject = 0          # venue rejected on re-validation
        self.n_partial = 0         # filled less than we asked for
        self.n_rej = {"binance": 0, "oracle": 0, "book": 0, "killed": 0,
                      "size": 0, "one_shot": 0}
        self.n_clob = 0        # counters surfaced in the health log so a
        self.n_clob_err = 0    # silently-stalled feed is visible at a glance
        self.n_clob_drop = 0   # queue-full drops: book desync until resync
        self.n_resub = 0       # subscription cycles == book resyncs
        self.n_eval_err = 0
        self.n_eval = 0
        self.n_signal = 0
        self.started_us = now_us()   # so a health line can be
                                     # attributed to THIS process
        self._seed_day_pnl()
        self._replay_open_positions()

    def _replay_open_positions(self):
        """Rebuild positions in STILL-OPEN markets from the fills log.

        A restart used to forget every position: the bot then saw zero
        shares in a market it already held max size in and could buy the
        full per-market cap AGAIN -- 2x the stated limit, invisible to
        every risk check. Fills for markets whose window has ended are
        left to the scorer; only open markets carry re-fire risk.
        """
        path = os.path.join(self.logdir, "paper_fills.jsonl")
        now_s = time.time()
        n = 0
        try:
            with open(path, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - 8 * 1024 * 1024))
                chunk = fh.read().decode("utf-8", "replace")
        except OSError:
            return
        lines = chunk.split("\n")
        for line in (lines[1:] if size > 8 * 1024 * 1024 else lines):
            if '"taker_fill"' not in line and '"maker_fill"' not in line \
                    and '"fill_correction"' not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if d.get("kind") not in ("taker_fill", "maker_fill",
                                     "fill_correction"):
                continue
            slug = d.get("slug", "")
            try:
                t1 = int(slug.rsplit("-", 1)[1]) + \
                    (300 if "-5m-" in slug else 900)
            except (ValueError, IndexError):
                continue
            if t1 <= now_s:
                continue
            pos = self.broker.positions.setdefault(
                (slug, d.get("side")), {"shares": 0.0, "cost": 0.0})
            if d.get("kind") == "fill_correction":
                # venue-verified adjustment (see _fill_truth): replaying
                # the raw fills without it would resurrect the corrupted
                # booking the correction fixed
                pos["shares"] = max(0.0, pos["shares"]
                                    + float(d.get("d_sh", 0)))
                pos["cost"] = max(0.0, pos["cost"]
                                  + float(d.get("d_cost", 0)))
                continue
            sh = float(d.get("shares", 0))
            px = float(d.get("px", 0))
            fee = float(d.get("fee_per_sh", 0))
            pos["shares"] += sh
            pos["cost"] += sh * (px + fee)
            n += 1
        if n:
            self.log_decision({"kind": "positions_replayed", "fills": n,
                               "markets": len({k[0] for k
                                               in self.broker.positions})})

    def _seed_day_pnl(self):
        """Rebuild today's realized P&L from the decision log on startup.

        Risk state lived only in memory, so a restart zeroed day_pnl and
        CLEARED the kill switch -- a bot that tripped its daily loss limit
        would resume trading the moment it was restarted, which live means
        the one safety rail against a broken edge can be undone by a
        deploy. Settle lines are already on disk; re-sum today's.
        """
        path = os.path.join(self.logdir, "decisions.jsonl")
        day_start_us = int(time.time() // 86400) * 86400 * 1_000_000
        total, outcomes, last_state = 0.0, [], None
        try:
            with open(path, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - 32 * 1024 * 1024))
                chunk = fh.read().decode("utf-8", "replace")
        except OSError:
            return
        lines = chunk.split("\n")
        anchor = None
        for line in (lines[1:] if size > 32 * 1024 * 1024 else lines):
            if '"settled"' not in line and '"risk_state"' not in line \
                    and '"day_anchor"' not in line \
                    and '"day_anchor_reloaded"' not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if d.get("t_us", 0) < day_start_us:
                continue
            if d.get("kind") in ("day_anchor", "day_anchor_reloaded"):
                # The day's FIRST anchor line of either kind: the balance
                # before today's losses. Accepting reload lines makes the
                # chain self-healing -- each restart re-writes one near
                # the tail, so the original surviving 32MB of churn is
                # not load-bearing.
                if anchor is None:
                    anchor = d
                continue
            if d.get("kind") == "risk_state":
                last_state = d
                continue
            if d.get("kind") != "settled":
                continue
            pnl = float(d.get("pnl", 0.0))
            total += pnl
            if pnl != 0:
                outcomes.append(pnl > 0)
        if anchor is not None and hasattr(self, "_day_bal_anchor"):
            self._day_bal_anchor = (anchor.get("day"),
                                    float(anchor.get("bal", 0.0)))
            self.log_decision({"kind": "day_anchor_reloaded",
                               "day": anchor.get("day"),
                               "bal": anchor.get("bal")})
        if outcomes or last_state:
            self.risk.seed(total, outcomes, logged=last_state)
            self.log_decision({"kind": "day_pnl_seeded",
                               "n_settles": len(outcomes),
                               "day_pnl": round(total, 2),
                               "state": self.risk.state_str()})

    def log_decision(self, obj):
        obj["t_us"] = now_us()
        if getattr(self, "mode", "paper") != "paper":
            obj["mode"] = self.mode   # lets reports split shadow-era from
                                      # live-era lines in the same file
        try:
            # Serialized: the event loop, the ops executor and the
            # fill-truth threads all write this handle; interleaved
            # writes tear lines, and a torn day_anchor line fails
            # json.loads on the next restart as silently as ENOSPC.
            # (module-level lock: one bot per process, per-coin flock)
            with _LOG_LOCK:
                self.decisions.write(json.dumps(obj, separators=(",", ":"))
                                     + "\n")
                self.decisions.flush()
        except OSError:
            # ENOSPC etc. A failed log write must degrade the RECORD, not
            # kill the process: an unguarded raise here turned a full disk
            # into a 5s crash loop across all five coins.
            self.n_log_err = getattr(self, "n_log_err", 0) + 1

    # ---- market discovery ---------------------------------------------
    async def discover(self, session):
        fams = self.cfg.get("families", {"15m": 900, "5m": 300})
        # One coin per process: BotState carries one oracle ring buffer, one
        # vol estimator and one basis series, and making all of that a dict
        # would touch every pricing path the reconciliation harness exists
        # to protect. Run `bot/run.py --coin eth` alongside for each coin.
        # Measured on the tape, btc-only is ~$580/day and the five-coin set
        # is $1.6-2.3k/day -- see reports/findings.md section 8.3.
        coin = self.coin
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
                m_new = MarketState(
                    slug, toks[0], ws * 1_000_000,
                    (ws + step) * 1_000_000,
                    asset_id_dn=toks[1] if len(toks) > 1 else None)
                # The venue declares the settlement window PER MARKET
                # (cryptoMarketConfig.twapLookbackSeconds), and it is not
                # uniform: zec 5m uses 60s where every other coin's 5m
                # uses 30s. Rule changes are announced on X only -- the
                # official changelog skipped the 2026-08-07 change
                # entirely -- so the declared config is the ONLY reliable
                # tripwire. Trust it over our duration-inferred default,
                # and refuse the market outright if TWAP is off or the
                # window is unrecognizable: trading a contract we have
                # not modelled is how this project's founding loss
                # happened to everyone else.
                cmc = mk.get("cryptoMarketConfig") or {}
                if isinstance(cmc, str):
                    try:
                        cmc = json.loads(cmc)
                    except Exception:  # noqa: BLE001
                        cmc = {}
                look = cmc.get("twapLookbackSeconds")
                if cmc and cmc.get("twapEnabled") is False:
                    self.log_decision({"kind": "contract_mismatch",
                                       "slug": slug,
                                       "note": "twapEnabled false; refusing"})
                    continue
                if look:
                    try:
                        lw = float(look)
                    except (TypeError, ValueError):
                        lw = None
                    if lw and lw != m_new.w:
                        self.log_decision({"kind": "contract_window",
                                           "slug": slug,
                                           "declared_w": lw,
                                           "assumed_w": m_new.w})
                        if lw in (30.0, 60.0):
                            m_new.w = lw
                        else:
                            continue    # unmodelled window: do not trade
                # A position replayed from before a restart counts toward
                # the claim ledger too, or the restarted broker could
                # re-claim liquidity the pre-restart process already took.
                for side in ("Up", "Down"):
                    held = self.broker.positions.get(
                        (slug, side), {}).get("shares", 0.0)
                    if held:
                        m_new.claimed[side] = held
                self.state.markets[slug] = m_new
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
            # LIVE money is never scored on our own oracle math: it
            # disagreed with the venue's resolution once on launch day
            # (same data gap that froze the book), and a mis-scored
            # settle biases the daily stop optimistic. Live positions
            # defer to the settle loop's Gamma path -- the venue's own
            # outcome, ~2 minutes slower and authoritative.
            result = None if self.mode == "live" else self._settle_result(m)
            if result is not None:
                pnl = self.broker.settle(slug, result)
                self.on_market_settled(pnl)
                self.log_decision({"kind": "settled", "slug": slug,
                                   "result": result, "pnl": pnl,
                                   "src": "oracle"})
                if self.mode == "live":
                    # Our oracle math scored a real-money market; verify
                    # against the venue's own resolution in ~3 minutes.
                    # Observed live: we scored a Down win (+0.14 booked)
                    # on a market Polymarket resolved as a Down LOSS --
                    # a mis-scored settle biases the kill switch in the
                    # dangerous (optimistic) direction.
                    self._settle_verify.append(
                        (slug, result, now_us() + 180_000_000))
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
        r_ps, r_secs, nticks, covered, hole = self.state._settle_full(
            m, m.t1_us)
        # The old guard (r_secs < 0.9w) could never fire: _integral
        # returns secs = the full span whenever ANY tick covers the
        # window start, so a settle window bridging a feed outage was
        # graded on a held (fabricated) price and fed the loss limit and
        # streak breaker wrong outcomes. Require real coverage -- enough
        # ticks, no hole, and the feed alive PAST t1 -- else defer to the
        # settle loop's authoritative Gamma fallback.
        newest = self.state.oracle_hist[-1][0] if self.state.oracle_hist \
            else 0
        if (K is None or r_secs <= 0 or not covered or nticks < m.w / 3
                or hole > 5.0 or newest < m.t1_us):
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
        url = ("wss://data-stream.binance.vision/ws/"
               f"{self.state.binance_symbol.lower()}@trade")
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
                            # The round-age watchdog must run on EVERY
                            # frame, before the symbol filter: if the
                            # multiplexed stream keeps delivering other
                            # coins but drops OURS, nothing below this
                            # line executes for our symbol and the old
                            # in-filter check waited for the 600s cycle.
                            if (self.state.oracle_hist
                                    and self.state.oracle_age_s()
                                    > MAX_ROUND_AGE_S):
                                self.log_decision({
                                    "kind": "rtds_round_stale",
                                    "age_s": round(
                                        self.state.oracle_age_s(), 1)})
                                break
                            pay = d.get("payload", {})
                            if pay.get("symbol") != self.state.oracle_symbol:
                                continue
                            px = float(pay["value"])
                            ts = int(pay["timestamp"])
                            self.state.on_oracle(px, ts)
                            for m in self.state.markets.values():
                                if (m.end_px is None
                                        and ts * 1000 >= m.t1_us):
                                    m.end_px = px
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
            # Repair the hole this outage just left. The recorder holds an
            # independent RTDS connection and flushes every tick to
            # data/live/rtds within seconds; merging its capture converts
            # a bot-socket outage (the common case -- the two connections
            # fail independently) into a non-event instead of a window
            # the hole-guard refuses to price.
            try:
                n = self.state.backfill_oracle(lookback_s=180)
                self.log_decision({"kind": "oracle_backfilled",
                                   "hist": n})
            except Exception:  # noqa: BLE001
                pass

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
                    self.n_resub += 1
                    subbed = set(ids)
                    t_end = time.time() + 240
                    try:
                        while time.time() < t_end:
                            # A market discovered mid-cycle was waiting up
                            # to 240s for its first book. A 5m market only
                            # lives 300s, so that could cost most of its
                            # tradable life. Break out and resubscribe as
                            # soon as the set changes.
                            live = set()
                            for _m in self.state.markets.values():
                                live.add(_m.asset_id_up)
                                if _m.asset_id_dn:
                                    live.add(_m.asset_id_dn)
                            if live - subbed:
                                break
                            msg = await asyncio.wait_for(ws.recv(), timeout=30)
                            try:
                                queue.put_nowait(msg)
                            except asyncio.QueueFull:
                                # A dropped price_change leaves the level
                                # map wrong until the next full snapshot,
                                # so the bot can price and fill against a
                                # book that no longer exists. Break the
                                # cycle NOW to force a fresh snapshot
                                # instead of trading a known-desynced
                                # book for up to 240s.
                                self.n_clob_drop += 1
                                self.log_decision(
                                    {"kind": "clob_drop_resync"})
                                break
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
            side = ev.get("side")
            # Aggressor direction in Up-token terms. A down-token BUY is,
            # mirrored, an aggressive SALE of Up -- the direction flips
            # with the price. printed() needs this to stop counting
            # opposite-direction flow as fillable liquidity.
            buy_up = None
            if side in ("BUY", "SELL"):
                buy_up = (side == "BUY") != mirror
            if mirror:
                px = 1.0 - px
            self.state.on_trade(up_aid, px, ev.get("timestamp"), sz,
                                buy_up=buy_up)
            self.broker.on_trade_print(up_aid, px, sz, now_us())
            if self.maker_broker is not None:
                # real prints drive the maker sim's fills: a print below
                # our resting level is proof a real trade swept it
                self.maker_broker.on_trade_print(up_aid, px, sz, now_us())

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
                try:
                    await self._settle_pass(session, give_up_s)
                except Exception as e:  # noqa: BLE001
                    # One bad pass must not kill the coroutine: main()'s
                    # gather tears down the whole process on the first
                    # unhandled exception in any task.
                    self.log_decision({"kind": "settle_loop_err",
                                       "err": repr(e)[:200]})

    async def _settle_pass(self, session, give_up_s):
        for item in list(getattr(self, "_settle_verify", []) or []):
            slug, res, due = item
            if now_us() < due:
                continue
            self._settle_verify.remove(item)
            truth = await self._gamma_result(session, slug)
            if truth is None:
                continue        # venue not resolved yet or voided
            if truth != res:
                self.log_decision({
                    "kind": "settle_MISMATCH", "slug": slug,
                    "our_result": res, "venue_result": truth,
                    "note": "P&L and the halt ladder were fed OUR oracle "
                            "score; the venue disagrees. Day risk state "
                            "is suspect until reviewed."})
            else:
                self.log_decision({"kind": "settle_verified",
                                   "slug": slug, "result": res})
        for slug, m in list(self.pending_settle.items()):
            result = None if self.mode == "live" \
                else self._settle_result(m)
            src = "oracle"
            if result is None and now_us() > m.t1_us + 120_000_000:
                result = await self._gamma_result(session, slug)
                src = "gamma"
            if result is not None:
                pnl = self.broker.settle(slug, result)
                self.on_market_settled(pnl)
                self.log_decision({"kind": "settled", "slug": slug,
                                   "result": result, "pnl": pnl,
                                   "src": src})
                if self.maker_broker is not None:
                    mpnl = self.maker_broker.settle(slug, result)
                    if mpnl != 0:
                        # isolated measurement -- NEVER fed into risk
                        self.log_decision({"kind": "maker_sim_settled",
                                           "slug": slug,
                                           "pnl": round(mpnl, 4)})
                if self.mode == "live" and src == "oracle":
                    self._settle_verify.append(
                        (slug, result, now_us() + 180_000_000))
                self.pending_settle.pop(slug, None)
            elif now_us() > m.t1_us + give_up_s * 1_000_000:
                if self.mode == "live":
                    # A LIVE market still unresolved after an hour is,
                    # in practice, the venue's voided-market remedy:
                    # shares redeem at $0.50 (gamma reports mid
                    # outcomePrices, which _gamma_result deliberately
                    # returns None for). The old path popped the
                    # position with NO pnl -- real money leaking out of
                    # the risk accounting entirely. Book the venue's
                    # remedy and feed the halt ladder.
                    pnl = 0.0
                    for k in [k for k in self.broker.positions
                              if k[0] == slug]:
                        pos = self.broker.positions.pop(k)
                        payoff = pos["shares"] * 0.5
                        pnl += payoff - pos["cost"]
                        self.broker._emit(
                            "settle", slug=slug, side=k[1],
                            shares=pos["shares"], cost=pos["cost"],
                            payoff=payoff, won=None)
                    self.on_market_settled(pnl)
                    self.log_decision({"kind": "settled", "slug": slug,
                                       "result": None,
                                       "pnl": round(pnl, 4),
                                       "src": "voided_0.5",
                                       "note": "unresolved 1h after "
                                               "close; booked at the "
                                               "void remedy 0.50/sh"})
                    if self.maker_broker is not None:
                        # same remedy for the isolated maker sim, else
                        # its positions leak and skew the measurement
                        for k in [k for k in self.maker_broker.positions
                                  if k[0] == slug]:
                            mp = self.maker_broker.positions.pop(k)
                            self.log_decision({
                                "kind": "maker_sim_settled", "slug": slug,
                                "pnl": round(mp["shares"] * 0.5
                                             - mp["cost"], 4),
                                "src": "voided_0.5"})
                else:
                    # Paper: free the concurrency slot. An unscored
                    # position left in broker.positions counts toward
                    # max_concurrent_markets FOREVER -- four of these
                    # and the bot stops trading entirely. The fills
                    # stay in paper_fills.jsonl and score_paper.py
                    # scores them independently off Gamma, so dropping
                    # the in-memory entry loses only the stuck slot.
                    for k in [k for k in self.broker.positions
                              if k[0] == slug]:
                        self.broker.positions.pop(k, None)
                    self.log_decision({"kind": "settle_FAILED",
                                       "slug": slug,
                                       "note": "position unscored here; "
                                               "slot freed, scorer will "
                                               "still see the fills"})
                self.pending_settle.pop(slug, None)

    def on_market_settled(self, pnl):
        """Route one settled market's P&L into risk and LOG any risk-state
        transition it causes. The transition line is what lets a restart
        reconstruct a probe-kill or an in-flight cool-off -- day_pnl alone
        cannot distinguish 'killed by the $400 backstop' from 'killed by a
        failed probe at -$180', and reconstructing the wrong one lets a
        deploy resume a confirmed-broken edge.
        """
        before = self.risk.state_str()
        self.risk.on_settle_pnl(pnl)
        after = self.risk.state_str()
        if before.split("(")[0] != after.split("(")[0]:
            self.log_decision({"kind": "risk_state", "state": after,
                               "day": self.risk.day,
                               "killed": self.risk.killed,
                               "halt_until_us": self.risk.halt_until_us,
                               "probe_left": self.risk.probe_left,
                               "probe_losses": self.risk.probe_losses_seen,
                               "day_pnl": round(self.risk.day_pnl, 2)})

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
            v = float(op[iu])
        except (TypeError, ValueError):
            return None
        # A voided/50-50 resolution (the venue's remedy for an oracle
        # outage) is NEITHER side winning; booking it as a Down win would
        # feed the loss limit and streak breaker a fictional outcome.
        # Leave it unsettled here; score_paper books it at 0.50/share.
        if 0.01 < v < 0.99:
            return None
        return 0 if v > 0.5 else 1

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
            try:
                self._health_line()
            except Exception as e:  # noqa: BLE001
                self.n_eval_err += 1
                self.log_decision({"kind": "health_err",
                                   "err": repr(e)[:200]})

    def _health_line(self):
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
            "kind": "health", "mode": self.mode, "markets": len(s.markets),
            "uptime_s": round((now_us() - self.started_us) / 1e6, 1),
            "pending_settle": len(self.pending_settle),
            "pending_orders": len(self.pending),
            "inflight": len(self.inflight), "misses": self.n_miss,
            "orders_sent": self.n_sent,
            "venue_rejects": self.n_reject, "partials": self.n_partial,
            "miss_why": dict(self.n_miss_why),
            "latency_ms": self.latency_us // 1000,
            "rejects": dict(self.n_rej),
            "clob_evs": self.n_clob, "clob_errs": self.n_clob_err,
            "clob_drops": self.n_clob_drop, "resubs": self.n_resub,
            "evals": self.n_eval, "eval_errs": self.n_eval_err,
            "log_errs": getattr(self, "n_log_err", 0),
            "signals": self.n_signal, "killed": self.risk.killed,
            "risk_state": self.risk.state_str(),
            "halts": self.risk.halts,
            "funnel": next((st.f for st in self.strategies
                            if hasattr(st, "f")), None),
            "funnels": {type(st).__name__: dict(st.f)
                        for st in self.strategies if hasattr(st, "f")},
            "coin": self.coin,
            "day_pnl": round(self.risk.day_pnl, 2),
            "oracle_hist": len(s.oracle_hist),
            "oracle_rate": s.oracle_rate(), "basis_n": len(s.basis),
            "price_rej": dict(s.n_price_rej),
            # Report the sigma that ACTUALLY enters z, and which
            # source it came from. The health line used to show only
            # s.vol.var -- the Binance-fed estimator -- while
            # sigma_rel() prefers the oracle-derived one. On this host
            # the Binance figure read 2.5e-06 for btc against a true 1s
            # sd near 1.3e-05, so the gauge was 5x off and pointed at a
            # number that was not in play. Sigma sits in the
            # DENOMINATOR of z; a wrong reading here is the exact shape
            # of a bug that already cost this project once.
            "sigma_used": s.sigma_rel(),
            "sigma_src": ("oracle" if s.oracle_sigma_rel() is not None
                          else ("binance" if s.vol.ok() else "NONE")),
            "sigma_binance": (s.vol.var ** 0.5) if s.vol.var else None,
            "vol_var": s.vol.var, "binance_px": s.binance_px,
            "oracle_px": s.oracle_px, "spot_adj": s.spot_adj(),
            "stale": {k: round(v, 1) for k, v in s.staleness().items()},
            "detail": mk})

    def _process_pending(self):
        """Execute delayed orders against the book as it is NOW.

        Four things stand between a signal and a fill, and the broker used
        to grant all four for free:

        * the venue holds crypto up/down taker orders 250 ms before matching
          and the network adds a round trip, so we act on a stale book;
        * we do not win the whole displayed size -- recorded books say the
          price we aimed at is still reachable only ~65% of the time in the
          0.92-0.99 region, and about half the size survives 400 ms;
        * size beyond the touch pays worse prices, so a big order walks up;
        * a displayed ask is an offer, a print is a completed trade. Fills
          are capped at a share of what actually printed at our limit.
        """
        if not self.pending:
            return
        if self.risk.killed or self.risk.in_cooloff():
            # The daily-loss kill switch blocks new SIGNALS via inputs_ok,
            # but orders already inside the latency window would still land.
            # A real kill has to stop those too -- live, this is the cancel
            # the venue's 250ms hold does not allow, so paper must not
            # grant it either... but a killed bot that keeps executing its
            # queue for another 400ms after the limit trips is the bigger
            # lie. Drop them and say so.
            self.log_decision({"kind": "pending_dropped_killed",
                               "n": len(self.pending)})
            self.pending = []
            return
        now = now_us()
        keep = []
        for o in self.pending:
            if now < o["fire_us"]:
                keep.append(o)
                continue
            self._execute(o, now)
        self.pending = keep

    def _execute(self, o, now):
        f = self.fill_cfg
        m = self.state.markets.get(o["slug"])
        if m is None:
            self.n_miss += 1
            self.n_miss_why["no_market"] += 1
            return
        # The venue re-validates when the hold expires and rejects on any
        # failed check; connection drops and matching-engine restarts land
        # here too. Modelled as a flat hazard rather than pretended away.
        if f["reject_rate"] > 0 and random.random() < f["reject_rate"]:
            self.n_reject += 1
            self.log_decision({"kind": "taker_reject", "slug": o["slug"],
                               "side": o["side"], "limit": o["limit"]})
            return
        # A fill can only be claimed against a view of the book that is
        # actually current. The CLOB feed goes deliberately blind for
        # ~1-2s at every resubscribe cycle and after a queue-full drop;
        # an order firing inside such a gap would "fill" against asks
        # that may have been pulled seconds ago -- live, that FAK misses.
        src_us = m.book_dn_us if (o["side"] == "Down" and m.asks_dn) \
            else m.book_us
        if not src_us or (now - src_us) / 1e6 > 1.5:
            self.n_miss += 1
            self.n_miss_why["stale_book"] = \
                self.n_miss_why.get("stale_book", 0) + 1
            self.log_decision({"kind": "taker_miss", "slug": o["slug"],
                               "side": o["side"], "limit": o["limit"],
                               "why": "book_view_stale",
                               "age_s": round((now - src_us) / 1e6, 2)
                               if src_us else None})
            return
        legs_avail = m.depth(o["side"], o["limit"])
        if not legs_avail:
            self.n_miss += 1
            self.n_miss_why["ask_gone"] += 1
            self.log_decision({"kind": "taker_miss", "slug": o["slug"],
                               "side": o["side"], "limit": o["limit"],
                               "why": "no_depth_at_limit"})
            return
        # cap 1: we are one of many takers racing the same quote
        budget = o["size"] * 1.0
        cap_book = sum(sz for _p, sz in legs_avail) * f["participation"]
        # cap 2: never claim more than the market actually absorbed in the
        # last few seconds -- a displayed ask is an offer, a print is proof
        cap_tape = f["vol_participation"] * m.printed(
            o["side"], o["limit"], now - int(f["vol_window_s"] * 1e6))
        # cap 3: never claim more than our share of what printed INSIDE
        # the settle window, minus what we already claimed. consume()
        # empties a level but the venue's next snapshot restores it (our
        # paper fill never happened out there), and the rolling 5s window
        # in cap 2 lets one print justify a fill again a second later --
        # so without a cumulative ledger the same displayed liquidity can
        # be eaten several times per market. Windowed to [t1-w, now):
        # counting the market's WHOLE life let a 15m market's fourteen
        # pre-window minutes of flow certify claims made in its last 60s,
        # a ~5x dilution of the cap this exists to be.
        win_start_us = m.t1_us - int(m.w * 1e6)
        if now_us() < win_start_us:
            # Open-window order (EarlyBird's paper leg): the settle
            # window is still in the FUTURE, so printed() over it is
            # zero by construction and every order died as too_small.
            # Cap by the flow that actually printed since the open.
            win_start_us = m.t0_us
        cap_life = (f["vol_participation"]
                    * m.printed(o["side"], o["limit"], win_start_us)
                    - m.claimed[o["side"]])
        want = min(budget, cap_book, cap_tape, cap_life) \
            if f["use_tape_cap"] else min(budget, cap_book)
        if want < f["min_order_size"]:
            self.n_miss += 1
            self.n_miss_why["too_small"] += 1
            self.log_decision({"kind": "taker_miss", "slug": o["slug"],
                               "side": o["side"], "limit": o["limit"],
                               "why": "below_min_size", "want": round(want, 2),
                               "cap_book": round(cap_book, 1),
                               "cap_tape": round(cap_tape, 1),
                               "cap_life": round(cap_life, 1)})
            return
        legs = []
        left = want
        for p, sz in legs_avail:
            take = min(left, sz * f["participation"])
            if take > 0:
                legs.append((p, take))
                left -= take
            if left <= 1e-9:
                break
        got = sum(s for _p, s in legs)
        if got <= 0:
            self.n_miss += 1
            self.n_miss_why["too_small"] += 1
            return
        if got < o["size"] - 1e-9:
            self.n_partial += 1
        vwap = sum(p * s for p, s in legs) / got
        meta = dict(o["meta"])
        meta["vwap"] = round(vwap, 5)
        meta["requested"] = o["size"]
        meta["cap_book"] = round(cap_book, 1)
        meta["cap_tape"] = round(cap_tape, 1)
        self.broker.taker_fills(o["slug"], o["side"], legs, meta=meta)
        # Our own take removes that liquidity: the next order in this same
        # millisecond must not find it again.
        for p, s in legs:
            m.consume(o["side"], p, s)
        m.claimed[o["side"]] += got

    def _maybe_eval(self, m):
        """Evaluate immediately inside the settle window -- and, when
        EarlyBird is registered, inside the opening seconds too."""
        if m is None:
            return
        rem = (m.t1_us - now_us()) / 1e6
        self._process_pending()
        if 0 < rem <= m.w:
            self._try_market(m)
            return
        if self._open_eval_s:
            elapsed = (now_us() - m.t0_us) / 1e6
            if 0 < elapsed <= self._open_eval_s:
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
            # Attribute the rejection to something inputs_ok ACTUALLY tests.
            # This used to check Binance staleness first, which stopped
            # being a rejection reason when the oracle became the fallback
            # -- so every oracle/book rejection was reported as "binance"
            # for as long as the throttled public mirror stayed stale, i.e.
            # nearly always. A diagnostic that names the wrong cause is
            # worse than no diagnostic.
            if self.risk.killed or self.risk.in_cooloff():
                self.n_rej["killed"] += 1
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
            # Pending orders are exposure too: z can flip sign inside the
            # ~400ms latency window (late in the window sd collapses, so a
            # $5-10 spot move swings z across both gates), and an Up order
            # still in the queue is invisible to broker.positions. Checking
            # only settled positions let the bot queue Up AND Down in the
            # same market and pay ~1.9 for a 1.0 payoff.
            if (self.broker.positions.get((m.slug, other),
                                          {}).get("shares", 0) > 0
                    or any(o["slug"] == m.slug and o["side"] == other
                           for o in self.pending + self.inflight)
                    # an unresolved (pending/unknown) order leaves
                    # inflight before its fate is known; its refire
                    # block is the only trace -- and it is exposure
                    or getattr(self, "_refire_block", {}).get(
                        (m.slug, other), 0) > now_us()):
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
            q_sh = sum(o["size"] for o in self.pending + self.inflight
                       if o["slug"] == m.slug and o["side"] == sig["side"])
            q_usd = sum(o["size"] * o["limit"]
                        for o in self.pending + self.inflight
                        if o["slug"] == m.slug and o["side"] == sig["side"])
            pos = {"shares": pos["shares"] + q_sh,
                   "cost": pos["cost"] + q_usd}
            if sig.get("one_shot") and pos["shares"] > 0:
                # one entry per market for this strategy: a partial fill
                # must never top up at a worse price (that profile was
                # never measured). Counted, not silent: the funnel's
                # `fired` keeps growing while the position is held, and
                # a zero-trade postmortem must be able to tell healthy
                # suppression from breakage.
                self.n_rej["one_shot"] += 1
                continue
            nmk = len({k[0] for k, v in self.broker.positions.items()
                       if v["shares"] > 0}
                      | {o["slug"] for o in self.pending + self.inflight})
            px = sig.get("level", sig.get("px", 0.5))
            if self.mode in ("live", "shadow") \
                    and sig.get("action") == "taker_buy":
                # Size against the PADDED limit, not the ask we saw. A
                # BUY here is rate-based: makerAmount = size x limit is
                # the cash committed, so sizing on the unpadded price
                # and sending a padded limit quietly commits more than
                # the per-market dollar cap allows. Pad first, size
                # second, and the cap binds on the money that can
                # actually leave. (Price improvement then returns MORE
                # shares for those same dollars -- the good direction,
                # and bounded by the cash, which is what can be lost.)
                px = self._pad_limit(sig["px"], sig.get("fair"))
                sig["limit_px"] = px
            size = self.risk.size_ok(pos["shares"], pos["cost"],
                                     sig["size"], px, nmk)
            if size <= 0:
                # Concurrency/cap starvation was SILENT: a signal sized to
                # zero (e.g. two unsettled markets holding both concurrent
                # slots at the next open) left no trace, which reads as
                # "no signal" in a zero-trade postmortem. Count it.
                self.n_rej["size"] += 1
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
                mb = self.maker_broker or self.broker
                # cancel-and-replace: one live quote per market. Without
                # this a reprice stacks resting orders and the sim fills
                # multiples of the intended size.
                mb.cancel_all(m.slug)
                mb.maker_buy(
                    m.slug, m.asset_id_up, sig["side"], sig["level"], size,
                    sig.get("queue_ahead", 0),
                    now_us() + int(sig.get("ttl_s", 20) * 1e6),
                    meta={"reason": sig["reason"]})
            elif sig["action"] == "taker_buy":
                if self.mode == "live":
                    # Real order, sent NOW: the venue's own 250ms hold and
                    # our real rtt replace the simulated latency -- adding
                    # both would double the delay the paper model exists
                    # to imitate.
                    self._dispatch_live(m, sig, size)
                    continue
                # Do NOT fill at the price we just saw. A marketable order
                # reaches the venue `latency_ms` later and executes against
                # whatever is resting THEN, up to our limit. 63% of paper
                # P&L came from sub-0.85 dips, which are exactly the prices
                # that disappear fastest -- filling them instantly is the
                # single biggest way paper flatters reality.
                self.n_sent += 1
                self.pending.append({
                    "slug": m.slug, "side": sig["side"],
                    "limit": sig.get("limit_px", sig["px"]), "size": size,
                    "fire_us": now_us() + self.latency_us,
                    "meta": {"reason": sig["reason"],
                             "oracle_age_s": sig.get("oracle_age_s"),
                             "z": sig.get("z"),
                             "ev_est": sig.get("ev_est"),
                             "seen_px": sig["px"]}})
                if self.mode == "shadow" and self.executor is not None:
                    # The execution-shadow record: exactly the order live
                    # mode would send, logged next to the paper fill the
                    # simulator books for the same signal. No network.
                    tok = m.asset_id_up if sig["side"] == "Up" \
                        else m.asset_id_dn
                    if tok:
                        # the PADDED limit: shadow exists to record what
                        # live would send, and live pads (see _pad_limit)
                        self.executor.submit_taker(
                            tok, "BUY", size,
                            sig.get("limit_px", sig["px"]), slug=m.slug,
                            outcome=sig["side"])

    # ---- live execution ------------------------------------------------
    def _dispatch_live(self, m, sig, size):
        """Send one real FAK for a sized signal, tracking it as exposure.

        The order enters `self.inflight` BEFORE the network call starts:
        the round trip takes ~0.5-1s and signals keep firing meanwhile, so
        an untracked in-flight order is exactly the invisible-exposure bug
        the pending-queue accounting exists to prevent.
        """
        tok = m.asset_id_up if sig["side"] == "Up" else m.asset_id_dn
        if not tok:
            self.n_miss += 1
            self.n_miss_why["no_market"] += 1
            self.log_decision({"kind": "live_miss", "slug": m.slug,
                               "side": sig["side"], "why": "no_token"})
            return
        now = now_us()
        if size < self.fill_cfg["min_order_size"]:
            # The venue's orderMinSize is 5 shares; risk room below that
            # cannot be sent. The market is effectively FULL for this
            # side, so block re-evaluation briefly too: every book tick
            # re-fires the same conclusion (observed: 10 identical
            # below_venue_min logs in 130ms).
            self.n_miss += 1
            self.n_miss_why["too_small"] += 1
            if now >= self._refire_block.get((m.slug, sig["side"]), 0):
                self.log_decision({"kind": "live_miss", "slug": m.slug,
                                   "side": sig["side"],
                                   "why": "below_venue_min",
                                   "size": round(size, 2)})
            self._refire_block[(m.slug, sig["side"])] = now + 5_000_000
            return
        if now < self._backoff_until:
            self.n_miss += 1
            self.n_miss_why["blocked"] = \
                self.n_miss_why.get("blocked", 0) + 1
            self.log_decision({"kind": "live_miss", "slug": m.slug,
                               "side": sig["side"], "why": "venue_backoff"})
            return
        if now < self._refire_block.get((m.slug, sig["side"]), 0):
            # an earlier order here has an unresolved outcome; skipping
            # silently -- the block itself was logged when it was set
            self.n_miss_why["blocked"] = \
                self.n_miss_why.get("blocked", 0) + 1
            return
        # A live order prices off OUR book view, and a desynced view is
        # how launch day's worst trade happened: the Down book froze at
        # 0.99 through a 1013 resubscribe while the venue traded 0.022,
        # so the signal was priced 40x from reality. The paper path has
        # this guard in _execute; live dispatch must not bypass it.
        src_us = m.book_dn_us if (sig["side"] == "Down" and m.asks_dn) \
            else m.book_us
        if not src_us or (now - src_us) / 1e6 > 2.5:
            self.n_miss += 1
            self.n_miss_why["stale_book"] = \
                self.n_miss_why.get("stale_book", 0) + 1
            self.log_decision({"kind": "live_miss", "slug": m.slug,
                               "side": sig["side"],
                               "why": "book_view_stale",
                               "age_s": round((now - src_us) / 1e6, 2)
                               if src_us else None})
            return
        # DESYNC guard. It exists for one failure: our book view frozen
        # while the venue moved (launch day -- Down stuck at 0.99 while
        # the venue traded 0.022). Test that directly, against the
        # venue's own last print, instead of against the model.
        #
        # Using model disagreement was wrong and silently halved the
        # strategy. bot/calib.py's table is ASYMMETRIC -- p_up spans
        # [0.1833, 0.9159] -- so a Down contract can never be valued
        # above 1-0.1833 = 0.8167 however certain it is. Buying Down at
        # 0.99 therefore scores ev -0.174 and tripped a -0.10 threshold
        # every time, while the identical Up trade scored -0.075 and
        # passed. Measured live: 10 blocked_divergence events, all Down,
        # all at 0.97-0.99 -- every Down favourite in the endgame,
        # blocked before it reached the venue, while paper (which has no
        # such guard) happily traded them. That gap was mine, not the
        # market's.
        lt_px, lt_us = m.last_trade_px, m.last_trade_us
        if lt_px is not None and lt_us and (now - lt_us) / 1e6 <= 30.0:
            ref = lt_px if sig["side"] == "Up" else 1.0 - lt_px
            gap = abs(sig["px"] - ref)
            if gap > self.cfg.get("desync_gap", 0.15):
                if now >= self._refire_block.get((m.slug, sig["side"]), 0):
                    self.log_decision({"kind": "blocked_desync",
                                       "slug": m.slug, "side": sig["side"],
                                       "px": sig["px"],
                                       "last_trade": round(ref, 4),
                                       "gap": round(gap, 4),
                                       "trade_age_s": round(
                                           (now - lt_us) / 1e6, 1)})
                self._refire_block[(m.slug, sig["side"])] = now + 3_000_000
                return
        # already computed at sizing time so the dollar cap binds on it
        lim = sig.get("limit_px") or self._pad_limit(sig["px"],
                                                     sig.get("fair"))
        o = {"slug": m.slug, "side": sig["side"], "limit": lim,
             "size": float(size),
             "meta": {"reason": sig["reason"], "z": sig.get("z"),
                      "pad_c": round(100 * (lim - sig["px"]), 1),
                      "oracle_age_s": sig.get("oracle_age_s"),
                      "ev_est": sig.get("ev_est"), "seen_px": sig["px"]}}
        self.inflight.append(o)
        self.n_sent += 1
        asyncio.get_running_loop().create_task(self._live_roundtrip(o, tok))

    def _pad_limit(self, seen_px, fair):
        """Marketable-limit padding: the fix for one-way fill selection.

        A limit is a CAP, and a CLOB matches a taker at the RESTING
        maker's price -- so a limit above the ask pays the ask, not the
        limit. This venue additionally re-validates the order after its
        250ms taker hold (docs: crypto up/down markets), which means a
        limit set to EXACTLY the ask we saw dies to any uptick during
        the round trip.

        That failure is not symmetric, and that is the whole problem: an
        unpadded taker fills when the price ticks DOWN (the market moved
        against our thesis) and is killed when it ticks UP (our thesis
        was right). Live measured the result directly -- 171 kills to 14
        fills, and fills averaging 0.296 against paper's 0.857. Padding
        removes the asymmetry; measured on 8 days of real prints it
        lifts the fill rate from 67% to ~90% with no EV decay, because
        the fills it adds are the ones we were being denied for being
        right.

        Bounded by value, never by hope: the pad is capped so that even
        paying the limit IN FULL leaves `pad_min_edge` of modelled edge
        after the venue's 0.07*p*(1-p) fee. With no fair value we do not
        pad at all.
        """
        pad = float(self.cfg.get("taker_pad", 0.0) or 0.0)
        if pad <= 0 or fair is None:
            return seen_px
        floor = float(self.cfg.get("taker_pad_min_edge", 0.005))
        cap = float(self.cfg.get("taker_pad_max_price", 0.99))
        # Bound the pad RELATIVE to the price as well as absolutely: a
        # flat 5c on a 3c ask is a 167% overpay allowance, and most of
        # this strategy's entries are cheap. Measured, the relative cap
        # keeps every bit of the fill-rate gain (+5.82c/sh t=3.15 vs
        # +5.51c/sh t=3.02 flat) while capping what a bad print can
        # cost. One tick is always allowed -- that is the whole point.
        rel = float(self.cfg.get("taker_pad_rel", 0.25))
        pad = min(pad, max(0.01, rel * seen_px))
        lim = min(seen_px + pad, cap)
        while lim > seen_px:
            if fair - lim - 0.07 * lim * (1 - lim) >= floor:
                break
            lim -= 0.005
        return round(max(lim, seen_px), 4)

    async def _live_roundtrip(self, o, token):
        """Await one venue round trip in a worker thread and book reality.

        The SDK call blocks through the venue's 250ms hold and returns the
        final fill synchronously; running it on the event loop would stall
        every feed for the duration, so it goes through run_in_executor.
        Whatever ACTUALLY filled -- shares and all-in price from
        making/taking -- is booked into the same broker the paper model
        uses, so settlement, risk, the halt ladder and the scorer all run
        on real numbers with zero new code paths.
        """
        loop = asyncio.get_running_loop()
        try:
            r = await loop.run_in_executor(
                self._order_pool, lambda: self.executor.submit_taker(
                    token, "BUY", o["size"], o["limit"], slug=o["slug"],
                    outcome=o["side"]))
        except Exception as e:  # noqa: BLE001
            r = {"status": "error", "filled": 0.0,
                 "detail": repr(e)[:300]}
        finally:
            try:
                self.inflight.remove(o)
            except ValueError:
                pass
        st = r.get("status")
        filled = float(r.get("filled") or 0)
        if st in ("filled", "partial") and filled > 0:
            px = float(r.get("avg_px") or o["limit"])
            meta = dict(o["meta"])
            meta.update(requested=o["size"], live=True,
                        order_id=r.get("order_id"))
            # The venue charges the 0.07*p*(1-p) taker fee ON TOP of the
            # matched amount, in collateral: on the first live fills the
            # account's cash out exceeded making_amount by exactly the
            # fee ($9.91 vs 9.90, $5.12 vs 5.09). Let the broker add the
            # modelled fee so booked cost matches the cash that left.
            self.broker.taker_fills(o["slug"], o["side"], [(px, filled)],
                                    meta=meta)
            mkt = self.state.markets.get(o["slug"])
            if mkt is not None:
                mkt.claimed[o["side"]] += filled
            if st == "partial":
                self.n_partial += 1
            if r.get("order_id"):
                # EVERY fill gets verified against the venue's trade
                # records: responses can echo the order instead of the
                # real fill (seen live: echoed 15 @ 0.99, actual 393 @
                # ~0.025 -- the async pipeline's match-intent behavior),
                # and bookings drive the risk engine, so they must match
                # reality. _fill_truth corrects the open position when
                # they differ.
                self._booked_oids.add(str(r.get("order_id")))
                loop.run_in_executor(None, self._fill_truth,
                                     r.get("order_id"), o["slug"],
                                     o["side"], token, px, filled)
                sc = (o.get("meta") or {}).get("scalp")
                if sc is not None and hasattr(self, "_scalps"):
                    # A scalp that is not exited becomes exactly the
                    # held-to-expiry bet this strategy exists to avoid,
                    # so the position is registered the moment it fills.
                    k = (o["slug"], o["side"])
                    prev = self._scalps.get(k)
                    self._scalps[k] = {
                        "slug": o["slug"], "side": o["side"],
                        "token": token, "entry": px,
                        "shares": filled + (prev["shares"] if prev else 0.0),
                        "target": sc["target"],
                        "deadline_us": sc["deadline_us"], "tries": 0}
        elif st == "killed":
            self.n_miss += 1
            self.n_miss_why["live_killed"] = \
                self.n_miss_why.get("live_killed", 0) + 1
            self.log_decision({"kind": "taker_miss", "slug": o["slug"],
                               "side": o["side"], "limit": o["limit"],
                               "why": "live_fak_killed"})
        elif st in ("pending", "unknown"):
            # Venue may still execute this order (collateral reserved; a
            # dropped connection does not stop it). Block re-fires on
            # this (slug,side) until its fate is KNOWN -- a fixed short
            # block was a double-fill path (order fills at t+3s, block
            # expires at t+10s, same signal re-fires and doubles the
            # position while the unbooked half stays invisible to risk)
            # -- and RECONCILE: _resolve_unknown polls the venue's own
            # trade records and books whatever actually happened.
            self._refire_block[(o["slug"], o["side"])] = \
                now_us() + 90_000_000
            loop.run_in_executor(None, self._resolve_unknown,
                                 r.get("order_id"), o["slug"], o["side"],
                                 token)
            self.log_decision({"kind": f"live_{st}", "slug": o["slug"],
                               "side": o["side"],
                               "order_id": r.get("order_id"),
                               "detail": (r.get("detail") or "")[:200]})
        elif st == "backoff":
            # 429 / post-only maintenance window: venue said stand down,
            # not "this order was bad". Global pause, not an error.
            self._backoff_until = now_us() + 3_000_000
            self.log_decision({"kind": "live_backoff", "slug": o["slug"],
                               "detail": (r.get("detail") or "")[:200]})
        elif st == "rejected":
            self.n_reject += 1
            d = (r.get("detail") or "").lower()
            kind = "live_balance_reject" if any(
                w in d for w in ("balance", "allowance", "collateral",
                                 "fund")) else "live_reject"
            if kind == "live_balance_reject":
                # burst orders colliding with a 250ms hold's collateral
                # RESERVATION look like empty pockets; brief block, and
                # the ops balance line arbitrates real depletion
                self._refire_block[(o["slug"], o["side"])] = \
                    now_us() + 2_000_000
            self.log_decision({"kind": kind, "slug": o["slug"],
                               "side": o["side"],
                               "detail": (r.get("detail") or "")[:200]})
        else:
            # A hard error still gets a short block: firing again into
            # whatever broke the last submission rarely helps within a
            # couple of seconds, and the storm variant spams the log.
            self._refire_block[(o["slug"], o["side"])] = \
                now_us() + 3_000_000
            self.log_decision({"kind": "live_order_error", "slug": o["slug"],
                               "side": o["side"],
                               "detail": (r.get("detail") or "")[:200]})

    def _resolve_unknown(self, order_id, slug, side, token):
        """An order whose outcome was non-terminal (venue 'delayed' /
        'live', a timeout, a dropped connection) may STILL have traded:
        collateral was reserved and the venue finishes processing on its
        own. Poll the venue's trade records until the fate is known and
        book whatever actually happened -- an unbooked fill is invisible
        to the risk engine, the daily stop, and the opposite-side guard,
        and its (slug,side) block must not lift until this resolves.

        With an order_id, trades join on taker_order_id. Without one
        (transport error before a response), any recent trade on this
        token whose taker_order_id is not already booked is claimed --
        our orders are serialized, so an unattributed fresh trade on the
        token we just targeted is ours."""
        found_sh, found_cost, found_px = 0.0, 0.0, None
        oids_found = set()
        ok_pass = False
        for delay in (4.0, 8.0, 15.0, 30.0):
            time.sleep(delay)
            # Pass-LOCAL accumulation, published only after the iteration
            # completes cleanly: an exception mid-iteration (a malformed
            # row, a dropped connection between pages) must neither
            # re-add rows on the next pass nor -- on the FINAL pass --
            # book a partial view of the fill.
            t_sh, t_cost, t_px = 0.0, 0.0, None
            t_oids = set()
            # ok_pass tracks the LAST pass only: one clean-but-early
            # empty poll must not convert later total failure into a
            # confident "no fill" (trade rows lag; the +4s look is the
            # least trustworthy of the four).
            ok_pass = False
            try:
                n_seen = 0
                for tr in self._ops_client().list_account_trades(
                        token_id=str(token)):
                    n_seen += 1
                    if n_seen > 60:
                        break
                    t_oid = str(getattr(tr, "taker_order_id", ""))
                    if order_id is not None:
                        if t_oid != str(order_id):
                            continue
                    else:
                        if t_oid in self._booked_oids:
                            continue
                        age = time.time() - getattr(
                            tr, "matched_at").timestamp()
                        if age > 180:
                            continue
                    if "FAIL" in str(getattr(tr, "status", "")).upper():
                        continue
                    p = float(tr.price)
                    s = float(tr.size)
                    t_sh += s
                    t_cost += s * (p + 0.07 * p * (1 - p))
                    t_px = p
                    if t_oid:
                        t_oids.add(t_oid)
                found_sh, found_cost, found_px = t_sh, t_cost, t_px
                oids_found = t_oids
                ok_pass = True
                if found_sh > 0:
                    break
            except Exception as e:  # noqa: BLE001
                self.log_decision({"kind": "resolve_unknown_err",
                                   "slug": slug, "err": repr(e)[:150]})
        def _apply():
            oid_one = next(iter(oids_found)) if oids_found else None
            if found_sh > 0:
                # claim EVERY trade row's order id -- the order_id=None
                # path can legitimately gather rows from more than one
                # order, and an unclaimed id is exactly what a LATER
                # resolver would double-book.
                for o in oids_found:
                    self._booked_oids.add(o)
                px_eff = found_cost / found_sh
                self.broker.taker_fills(
                    slug, side, [(found_px or px_eff, found_sh)],
                    meta={"live": True, "resolved_from": "pending",
                          "order_id": str(order_id or oid_one)[:24]},
                    fee_per_sh=(found_cost / found_sh
                                - (found_px or px_eff)))
                mkt = self.state.markets.get(slug)
                if mkt is not None:
                    mkt.claimed[side] += found_sh
                self.log_decision({"kind": "unknown_order_FILLED",
                                   "slug": slug, "side": side,
                                   "sh": round(found_sh, 2),
                                   "cost": round(found_cost, 4)})
                # The pass that found rows saw them at MAXIMUM lag: a
                # 15-share order crossing two makers can show one row
                # first, and break-on-found books that partial view
                # permanently. Hand the booking to the verifier, which
                # re-polls at +4/12/30/90s and corrects the position
                # toward venue truth in BOTH directions.
                vid = order_id or oid_one
                if vid and self._loop is not None:
                    self._loop.run_in_executor(
                        None, self._fill_truth, vid, slug, side, token,
                        found_px or px_eff, found_sh)
            elif ok_pass:
                self.log_decision({"kind": "unknown_order_no_fill",
                                   "slug": slug, "side": side,
                                   "order_id": str(order_id)[:24]})
            else:
                # every poll pass FAILED -- the order's fate is genuinely
                # unknown (transport trouble is exactly correlated with
                # the failures that create unknowns). Do NOT lift the
                # block early; let the full 90s expire. A fill landing
                # later is caught by the cash floor and the next
                # operator look at this loud line.
                self.log_decision({"kind": "unknown_order_UNRESOLVED",
                                   "slug": slug, "side": side,
                                   "order_id": str(order_id)[:24],
                                   "note": "all polls failed; refire "
                                           "block left to expire"})
                return
            # fate known: lift the block
            self._refire_block.pop((slug, side), None)
        if self._loop is not None:
            self._loop.call_soon_threadsafe(_apply)

    async def live_keepalive_loop(self):
        """Keep the ORDER client's connection hot.

        Signals are minutes apart; the SDK hardcodes httpx
        keepalive_expiry=30s and Cloudflare reaps idle connections too,
        so a cold order pays TCP+TLS+HTTP/2 setup INSIDE the race the
        venue's 250ms hold already makes tight -- pure lost conversion.
        Every 20s (UNDER the 30s pool expiry; the first 45s interval sat
        just past it and warmed nothing), an authenticated GET that
        rides the SAME `secure_clob` transport post_order uses -- warming
        any other of the SDK's nine per-purpose httpx clients would keep
        the wrong socket alive.
        """
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(20)
            try:
                await loop.run_in_executor(
                    self._order_pool,
                    lambda: self.executor.client.get_closed_only_mode())
            except Exception:  # noqa: BLE001
                pass    # order errors are logged on their own path
            self._maybe_prewarm()

    def _maybe_prewarm(self):
        """Warm the SDK's per-token market-metadata cache BEFORE the
        firing window. create_limit_order is not pure local signing: on
        a cache miss it performs TWO blocking HTTP GETs (token->condition
        resolve + market record), and the cache TTL is ~10 minutes -- so
        without this, the FIRST order of every market pays the exact
        round trips the direct-posting build removed. A discarded
        sign-only order per token on the order thread, once per market,
        when its window is 2.5 minutes out."""
        now = now_us()
        # prune expired re-fire blocks while we're here
        for k in [k for k, v in self._refire_block.items() if v < now]:
            self._refire_block.pop(k, None)
        loop = asyncio.get_running_loop()
        for m in list(self.state.markets.values()):
            rem = (m.t1_us - now) / 1e6
            # warm EVERY known market, not just the endgame: EarlyBird's
            # first order lands seconds after OPEN, and a cold metadata
            # cache would put two blocking GETs back into exactly that
            # order. The cache TTL is ~10min, shorter than a 15m
            # market's life -- so RE-warm when the first warm has aged
            # past ~450s and a firing window is near (rem<=150), instead
            # of once-per-slug forever.
            if not (0 < rem <= 1000):
                continue
            warmed_at = self._prewarmed.get(m.slug)
            if warmed_at is not None:
                age_s = (now - warmed_at) / 1e6
                if age_s < 450 or rem > 150:
                    continue
            self._prewarmed[m.slug] = now
            for tok in (m.asset_id_up, m.asset_id_dn):
                if not tok:
                    continue

                def _warm(t=str(tok)):
                    try:
                        self.executor.client.create_limit_order(
                            token_id=t, side="BUY", price="0.01", size="5")
                    except Exception:  # noqa: BLE001
                        pass            # warming is best-effort
                loop.run_in_executor(self._order_pool, _warm)
        if len(self._prewarmed) > 64:
            self._prewarmed &= set(self.state.markets)

    async def scalp_exit_loop(self):
        """Close every scalp: at the target, or flat at the deadline.

        The strategy's whole risk claim is that it never carries a
        binary to settlement. That claim is false unless something
        actually sells, so this loop is the strategy -- an entry with a
        broken exit is a held-to-expiry bet at a coin-flip price.

        Two-stage exit. A sell RESTS at the target as soon as the entry
        fills; at the deadline it is cancelled and the remainder is
        crossed out with a marketable FAK.

        Resting was previously refused here on the grounds that queue
        position is unverified and is what made ZMaker lose 22.8c/share.
        That objection is right in principle, so it was measured instead
        of argued: scoring a resting exit as filled ONLY when a print
        goes strictly THROUGH the target -- i.e. assuming we are last in
        the queue at our own price -- the holdout is +1.44c/share against
        +1.04c for crossing out every time. The optimistic fill
        assumption gives +1.76c and is deliberately not relied on.

        The ordering matters for safety: cancel must be confirmed, and
        the order's final matched size re-read, BEFORE any FAK goes out.
        Crossing while a resting sell might still be live would sell the
        same shares twice, and the second sale would be naked.
        """
        while True:
            await asyncio.sleep(0.5)
            if not getattr(self, "_scalps", None):
                continue
            now = now_us()
            for k in list(self._scalps):
                p = self._scalps.get(k)
                if p is None or p["shares"] < 5.0:
                    self._scalps.pop(k, None)
                    continue
                due = now >= p["deadline_us"]
                loop = asyncio.get_running_loop()

                # --- stage 1: rest a sell at the target, once ---------
                if not due and not p.get("sell_oid") and not p.get("no_rest"):
                    r = await loop.run_in_executor(
                        self._order_pool,
                        lambda pp=p: self.executor.submit_maker_sell(
                            pp["token"], pp["shares"], pp["target"],
                            slug=pp["slug"], outcome=pp["side"]))
                    st = r.get("status")
                    if st == "resting":
                        p["sell_oid"] = r.get("order_id")
                        p["maker_booked"] = 0.0
                        self.log_decision({
                            "kind": "scalp_rested", "slug": p["slug"],
                            "side": p["side"], "shares": p["shares"],
                            "target": round(p["target"], 4),
                            "order_id": r.get("order_id")})
                    elif st in ("filled", "partial"):
                        # crossed on arrival: the bid was already above
                        # our target, so WE took and the fee applies
                        got = float(r.get("filled") or 0.0)
                        px = r.get("avg_px") or p["target"]
                        real = self.broker.taker_sell(p["slug"], p["side"],
                                                      px, got)
                        p["shares"] -= got
                        self.log_decision({
                            "kind": "scalp_exit", "slug": p["slug"],
                            "side": p["side"], "entry": round(p["entry"], 4),
                            "exit": px, "shares": got,
                            "pnl_c": round(100 * (px - p["entry"]), 2),
                            "realised": round(real, 4), "why": "cross_on_post"})
                        if p["shares"] < 5.0:
                            self._scalps.pop(k, None)
                    else:
                        # could not rest -- fall back to the taker path
                        # rather than carry the binary to settlement
                        p["no_rest"] = True
                        self.log_decision({
                            "kind": "scalp_rest_failed", "slug": p["slug"],
                            "side": p["side"], "status": st,
                            "detail": (r.get("detail") or "")[:120]})
                    continue

                # --- stage 2: has the resting sell been lifted? -------
                if p.get("sell_oid") and (due or now - p.get("last_poll", 0)
                                          >= 2_000_000):
                    p["last_poll"] = now
                    stt = await loop.run_in_executor(
                        self._order_pool,
                        lambda pp=p: self.executor.order_state(pp["sell_oid"]))
                    if stt is not None:
                        newly = stt["matched"] - p.get("maker_booked", 0.0)
                        if newly > 1e-9:
                            # maker fill: no venue fee. Booking it at the
                            # taker rate would cost 1.68c/share at 0.60,
                            # more than the edge.
                            real = self.broker.taker_sell(
                                p["slug"], p["side"],
                                stt["price"] or p["target"], newly,
                                fee_per_sh=0.0)
                            p["maker_booked"] = stt["matched"]
                            p["shares"] -= newly
                            self.log_decision({
                                "kind": "scalp_exit", "slug": p["slug"],
                                "side": p["side"],
                                "entry": round(p["entry"], 4),
                                "exit": stt["price"] or p["target"],
                                "shares": newly,
                                "pnl_c": round(100 * ((stt["price"]
                                                       or p["target"])
                                                      - p["entry"]), 2),
                                "realised": round(real, 4),
                                "why": "target_maker"})
                        if p["shares"] < 5.0:
                            self._scalps.pop(k, None)
                            continue

                # --- stage 3: deadline -- cancel, THEN cross out ------
                if due and p.get("sell_oid"):
                    ok = await loop.run_in_executor(
                        self._order_pool,
                        lambda pp=p: self.executor.cancel_order(
                            pp["sell_oid"]))
                    stt = await loop.run_in_executor(
                        self._order_pool,
                        lambda pp=p: self.executor.order_state(pp["sell_oid"]))
                    if stt is None and not ok:
                        # We do not know whether the order is dead or
                        # what it filled. Crossing now risks selling the
                        # same shares twice, so retry instead.
                        p["tries"] += 1
                        self.log_decision({
                            "kind": "scalp_exit_failed", "slug": p["slug"],
                            "side": p["side"], "shares": p["shares"],
                            "tries": p["tries"], "status": "cancel_unknown",
                            "detail": "cancel unconfirmed and state "
                                      "unreadable; not crossing"})
                        if p["tries"] >= 20:
                            self._scalps.pop(k, None)
                        continue
                    if stt is not None:
                        newly = stt["matched"] - p.get("maker_booked", 0.0)
                        if newly > 1e-9:
                            real = self.broker.taker_sell(
                                p["slug"], p["side"],
                                stt["price"] or p["target"], newly,
                                fee_per_sh=0.0)
                            p["maker_booked"] = stt["matched"]
                            p["shares"] -= newly
                            self.log_decision({
                                "kind": "scalp_exit", "slug": p["slug"],
                                "side": p["side"],
                                "entry": round(p["entry"], 4),
                                "exit": stt["price"] or p["target"],
                                "shares": newly, "realised": round(real, 4),
                                "why": "target_maker_late"})
                    p["sell_oid"] = None
                    if p["shares"] < 5.0:
                        self._scalps.pop(k, None)
                        continue

                # A resting sell is still live and we are not at the
                # deadline. Never ALSO cross out here: that sells the
                # same shares twice and the second sale is naked. The
                # only paths below are "we could not rest" (no_rest) and
                # "the resting order is cancelled and reconciled".
                if p.get("sell_oid"):
                    continue

                m = self.state.markets.get(p["slug"])
                bid = None
                if m is not None:
                    if p["side"] == "Up":
                        bid, _ = m.best_bid()
                    else:
                        ba, _bs = m.best_ask()
                        bid = (1.0 - ba) if (ba and 0 < ba < 1) else None
                due = now >= p["deadline_us"]
                hit = bid is not None and bid >= p["target"]
                if not (due or hit):
                    continue
                # floor: at the target take it; at the deadline accept
                # whatever the book pays rather than carry the binary
                floor = p["target"] if hit else max(0.01, (bid or 0.01)
                                                    - 0.02)
                p["tries"] += 1
                r = await asyncio.get_running_loop().run_in_executor(
                    self._order_pool,
                    lambda pp=p, fl=floor: self.executor.submit_taker_sell(
                        pp["token"], pp["shares"], fl, slug=pp["slug"],
                        outcome=pp["side"]))
                st = r.get("status")
                if st in ("filled", "partial"):
                    got = float(r.get("filled") or 0.0)
                    px = r.get("avg_px") or floor
                    # No hasattr guard: a missing broker method must
                    # crash loudly, not silently skip the booking. It
                    # did skip, and settle() then scored shares already
                    # sold -- phantom P&L straight into day_pnl, which
                    # is what the daily stop reads.
                    real = self.broker.taker_sell(p["slug"], p["side"],
                                                  px, got)
                    self.log_decision({
                        "kind": "scalp_exit", "slug": p["slug"],
                        "side": p["side"], "entry": round(p["entry"], 4),
                        "exit": px, "shares": got,
                        "pnl_c": round(100 * (px - p["entry"]), 2),
                        "realised": round(real, 4),
                        "why": "target" if hit else "deadline"})
                    p["shares"] -= got
                    if p["shares"] < 5.0:
                        self._scalps.pop(k, None)
                elif st == "shadow":
                    self._scalps.pop(k, None)
                else:
                    # Unsold and past the deadline is the failure mode
                    # that turns a scalp into a settlement bet. Say so
                    # loudly on every retry rather than once.
                    self.log_decision({
                        "kind": "scalp_exit_failed", "slug": p["slug"],
                        "side": p["side"], "shares": p["shares"],
                        "floor": round(floor, 4), "tries": p["tries"],
                        "status": st, "detail": (r.get("detail") or "")[:120]})
                    if p["tries"] >= 20:
                        self._scalps.pop(k, None)

    async def live_ops_loop(self):
        """Live-mode housekeeping every 5 minutes: redeem resolved
        winnings back into pUSD and log the spendable balance.

        Winnings arrive as conditional tokens; until redeemed they are
        dead capital, and with a micro bankroll the bot would run dry of
        collateral within hours and every order after that would come
        back `live_balance_reject`. The balance line in the decision log
        is the recycling gauge -- flat-while-winning means redemption is
        broken and this loop's errors say why.
        """
        loop = asyncio.get_running_loop()
        while True:
            # Pass FIRST, sleep after: the cash floor must be enforced
            # from startup, not 5 minutes in -- a crash-looping bot with
            # sub-5-min uptime would otherwise never check it at all,
            # and the reloaded day anchor would sit unused.
            try:
                bal, redeemed = await loop.run_in_executor(
                    None, self._live_ops_pass)
                self.log_decision({"kind": "live_balance", "usdc": bal,
                                   "redeemed": redeemed})
            except Exception as e:  # noqa: BLE001
                self.log_decision({"kind": "live_ops_err",
                                   "err": repr(e)[:200]})
            await asyncio.sleep(300)

    def _ops_client(self):
        """Housekeeping/verification client on its OWN HTTP connection:
        a redemption's transaction wait must never queue an order behind
        it, and sharing the order client's HTTP/2 connection across
        threads is what lost two orders to stream-table corruption on
        launch day."""
        if self._ops_ex is None:
            from bot.live import LiveExecutor
            self._ops_ex = LiveExecutor(
                shadow=False,
                log_path=os.path.join(self.logdir, "ops.jsonl"))
        return self._ops_ex.client

    def _fill_truth(self, order_id, slug, side, token, booked_px,
                    booked_sh):
        """Reconcile a booked fill against the venue's own trade records
        and CORRECT the open position whenever they differ.

        The order response is not authoritative: under the async commit
        pipeline it can echo the order (full size at exactly the limit)
        while the real trades executed at other sizes and prices --
        observed live as a booked 15 sh @ 0.99 whose actual trades were
        393 sh @ ~0.025. It checks at +4s, +12s, +30s and +90s (NOT
        single-shot: trade-row visibility lags the same async pipeline,
        and a trade shown MATCHED early can resolve FAILED later), each
        time correcting toward the venue's absolute truth. Bookings
        feed the risk engine and the daily stop; they must match cash."""
        applied_sh = booked_sh
        applied_cost = booked_sh * (booked_px
                                    + 0.07 * booked_px * (1 - booked_px))
        seen_any = False
        diverged = False
        for delay in (4.0, 8.0, 18.0, 60.0):
            time.sleep(delay)
            try:
                true_sh, true_cost = 0.0, 0.0
                statuses = []
                n_seen = 0
                for tr in self._ops_client().list_account_trades(
                        token_id=str(token)):
                    n_seen += 1
                    if n_seen > 100:
                        break
                    if str(getattr(tr, "taker_order_id", "")) \
                            != str(order_id):
                        continue
                    stt = str(getattr(tr, "status", "")).upper()
                    statuses.append(stt)
                    if "FAIL" in stt:
                        continue
                    p = float(tr.price)
                    s = float(tr.size)
                    true_sh += s
                    true_cost += s * (p + 0.07 * p * (1 - p))
                if not statuses:
                    continue        # rows not visible yet; retry
                seen_any = True
                d_sh = true_sh - applied_sh
                d_cost = true_cost - applied_cost
                if abs(d_sh) < 0.01 and abs(d_cost) < 0.02:
                    continue
                diverged = True
                applied_sh, applied_cost = true_sh, true_cost

                def _apply(d_sh=d_sh, d_cost=d_cost, true_sh=true_sh,
                           true_cost=true_cost, statuses=list(statuses)):
                    pos = self.broker.positions.get((slug, side))
                    applied = False
                    if pos is not None and pos.get("shares", 0) > 0:
                        pos["shares"] = max(0.0, pos["shares"] + d_sh)
                        pos["cost"] = max(0.0, pos["cost"] + d_cost)
                        applied = True
                        # Corrections must survive restarts: the replay
                        # rebuilds from the fills log, so an unlogged
                        # correction would revert on redeploy.
                        self.broker._emit("fill_correction", slug=slug,
                                          side=side, d_sh=round(d_sh, 4),
                                          d_cost=round(d_cost, 6),
                                          order_id=str(order_id)[:24])
                    self.log_decision({
                        "kind": "fill_truth_CORRECTED" if applied
                                else "fill_truth_MISMATCH_POST_SETTLE",
                        "slug": slug, "side": side,
                        "order_id": str(order_id)[:24],
                        "venue": {"sh": round(true_sh, 2),
                                  "cost": round(true_cost, 4),
                                  "statuses": statuses[:6]}})
                if self._loop is not None:
                    self._loop.call_soon_threadsafe(_apply)
            except Exception as e:  # noqa: BLE001
                self.log_decision({"kind": "fill_truth_err",
                                   "order_id": str(order_id)[:24],
                                   "err": repr(e)[:150]})
        if not seen_any:
            self.log_decision({"kind": "fill_truth_missing",
                               "slug": slug, "side": side,
                               "order_id": str(order_id)[:24],
                               "note": "no venue trade rows in 2 minutes;"
                                       " booking stands UNVERIFIED"})
        elif not diverged:
            self.log_decision({"kind": "fill_truth_ok", "slug": slug,
                               "side": side, "sh": round(applied_sh, 2)})

    def _live_ops_pass(self):
        cl = self._ops_client()
        bal = None
        try:
            b = cl.get_balance_allowance(asset_type="COLLATERAL")
            raw = float(getattr(b, "balance", 0) or 0)
            # raw 6-decimal units vs dollars; a real balance would need to
            # exceed $10k to fool this, two orders of magnitude past the
            # micro bankroll.
            bal = round(raw / 1e6 if raw > 1e4 else raw, 2)
        except Exception:  # noqa: BLE001
            pass
        if bal is not None:
            # CASH-ANCHORED daily kill floor, immune to bookkeeping. The
            # booked day_pnl missed a breached daily stop on launch day
            # (a phantom settle credited a win the venue never paid), so
            # the stop is also enforced against the one number that
            # cannot lie: the wallet. Floor = day-start balance minus
            # the daily limit minus the largest legitimate in-flight
            # exposure (concurrent markets x per-market cap), so normal
            # open positions can never false-trip it.
            day = time.strftime("%Y-%m-%d", time.gmtime())
            if self._day_bal_anchor is None \
                    or self._day_bal_anchor[0] != day:
                self._day_bal_anchor = (day, bal)
                self._anchor_logged_day = None
            elif bal > self._day_bal_anchor[1] + 150.0:
                # A rise no plausible trading day produces at this size
                # is a DEPOSIT: re-anchor upward (only ever tightens the
                # floor) so new money is protected by the same $-limit.
                # Revisit the 150 constant when per-trade size scales.
                self._day_bal_anchor = (day, bal)
                self._anchor_logged_day = None
                self.log_decision({"kind": "day_anchor_deposit",
                                   "day": day, "bal": bal})
            # Persist -- VERIFIED and retried: the anchor lived only in
            # memory, so a restart after a losing stretch re-based the
            # floor at the DRAINED balance. A single unverified write
            # was not enough either: log_decision swallows ENOSPC, and
            # a swallowed anchor line silently restores the old bug on
            # the next restart. Re-log on every ops pass until a write
            # succeeds; _seed_day_pnl reloads the day's FIRST anchor.
            if getattr(self, "_anchor_logged_day", None) != day:
                pre_err = getattr(self, "n_log_err", 0)
                self.log_decision({"kind": "day_anchor", "day": day,
                                   "bal": self._day_bal_anchor[1]})
                if getattr(self, "n_log_err", 0) == pre_err:
                    self._anchor_logged_day = day
            floor = (self._day_bal_anchor[1]
                     - abs(self.risk.daily_loss_limit)
                     - self.risk.max_concurrent
                     * self.risk.max_market_dollars)
            if bal < floor and not self.risk.killed:
                self.risk.killed = True
                self.log_decision({
                    "kind": "cash_kill", "bal": bal,
                    "anchor": self._day_bal_anchor[1],
                    "floor": round(floor, 2),
                    "note": "cash drawdown breached the book-independent"
                            " floor; killed for the rest of the UTC day"})
        conds, redeemed = [], 0
        try:
            for pos in cl.list_positions(redeemable=True):
                c = str(getattr(pos, "condition_id", "") or "")
                if c and c not in conds:
                    conds.append(c)
                if len(conds) >= 20:    # bound one pass; the next sweep
                    break               # is 5 minutes away
        except Exception as e:  # noqa: BLE001
            self.log_decision({"kind": "redeem_list_err",
                               "err": repr(e)[:150]})
        for c in conds:
            try:
                h = cl.redeem_positions(condition_id=c)
                getattr(h, "wait", lambda: None)()
                redeemed += 1
            except Exception as e:  # noqa: BLE001
                self.log_decision({"kind": "redeem_err", "cond": c[:18],
                                   "err": repr(e)[:150]})
        return bal, redeemed

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
        self._loop = asyncio.get_running_loop()
        tasks = [self.binance_feed(), self.rtds_feed(),
                 self.clob_feed(), self.decide_loop(),
                 self.discovery_loop(), self.health_loop(),
                 self.settle_loop()]
        if self.mode == "live":
            tasks.append(self.live_ops_loop())
            if getattr(self, '_scalps', None) is not None:
                tasks.append(self.scalp_exit_loop())
            tasks.append(self.live_keepalive_loop())
        await asyncio.gather(*tasks)


def acquire_lock(path):
    """Refuse to start a second instance FOR THE SAME COIN.

    Two bots sharing one paper_fills.jsonl double-count every fill and
    silently corrupt the paper record that the whole go/no-go decision
    rests on. The lock is per coin, so btc and eth run side by side while a
    second btc is still refused. An advisory flock is enough: it is
    released automatically if the process dies, so restarts stay clean.
    """
    import fcntl
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"another bot/run.py already holds {path} — exiting",
              file=sys.stderr)
        raise SystemExit(3)
    fh.write(str(os.getpid()))
    fh.flush()
    return fh                       # keep the handle alive for the process


if __name__ == "__main__":
    import argparse
    from bot.state import COINS
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", default=None, choices=sorted(COINS),
                    help="which coin this process trades "
                         "(default: config's `coin`)")
    ap.add_argument("--cfg", default=None,
                    help="alternate config file (e.g. bot/config.live.json);"
                         " config.local.json still merges over it")
    ap.add_argument("--mode", default=None,
                    choices=["paper", "shadow", "live"],
                    help="override the config's mode (ad-hoc testing only;"
                         " deployed units should get mode from the config)")
    args = ap.parse_args()
    cfg = load_cfg(args.cfg)
    if args.mode:
        cfg["mode"] = args.mode
    coin = args.coin or cfg.get("coin", "btc")
    # Shadow/live instances lock in their own tree: they run BESIDE the
    # paper bot for the same coin by design, not instead of it.
    lockdir = (f"logs/live/{coin}" if cfg.get("mode", "paper") != "paper"
               else f"logs/{coin}")
    _lock = acquire_lock(f"{lockdir}/bot.lock")
    bot = Bot(cfg, coin=coin)
    asyncio.run(bot.main())
