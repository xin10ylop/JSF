"""Online market state for the paper/live bot.

Maintains, from live feeds:
- Binance last price (from binance ws trade stream)
- Oracle (Chainlink via RTDS) last round + rolling basis ratio vs Binance
- Online EWMA variance of 1s Binance log returns (halflife 300s)
- Per-market: window times, strike (first oracle round >= t0), book state
  (top levels), last trade
- G(z) fair value via data/gz_models.pkl

All timestamps are microseconds. Staleness is tracked per input; consumers
must refuse to act when stale (risk module enforces).
"""
import json
import math
import pickle
import time
from collections import deque

import numpy as np


def now_us():
    return int(time.time() * 1_000_000)


class OnlineVol:
    """EWMA variance of 1s log returns, updated on each Binance second."""

    def __init__(self, halflife_s=300.0):
        self.alpha = 1 - math.exp(math.log(0.5) / halflife_s)
        self.var = None
        self.last_sec = None
        self.last_px = None

    def update(self, sec, px):
        """Update only on second boundaries so the estimator matches the
        backtest's strict 1s-grid EWMA (audit item: identical inputs)."""
        if self.last_sec is None:
            self.last_sec = sec
            self.last_px = px
            return
        if sec <= self.last_sec:
            self.last_px = px          # track latest price within the second
            return
        dt = sec - self.last_sec
        r = math.log(px / self.last_px)
        r2_per_s = (r * r) / dt
        for _ in range(min(int(dt), 10)):
            if self.var is None:
                self.var = r2_per_s
            else:
                self.var += self.alpha * (r2_per_s - self.var)
        self.last_sec = sec
        self.last_px = px


class GzPricer:
    def __init__(self, path=None, k_cal=None):
        if path is None:
            import os
            here = os.path.dirname(os.path.abspath(__file__))
            cand = [os.path.join(here, "gz_models.pkl"),
                    "data/gz_models.pkl"]
            path = next(p for p in cand if os.path.exists(p))
        with open(path, "rb") as f:
            obj = pickle.load(f)
        self.models = obj["models"]
        self.K = k_cal if k_cal is not None else obj["K"]

    def fair(self, spot, strike, var_rate, rem_s):
        if not (spot > 0 and strike > 0 and var_rate and var_rate > 0
                and rem_s > 0):
            return None
        z = math.log(spot / strike) / math.sqrt(self.K * var_rate * rem_s)
        z = max(-4.0, min(4.0, z))
        for (lo, hi), iso in self.models.items():
            if lo < rem_s <= hi:
                return float(iso.predict([z])[0])
        return None


class MarketState:
    """Post-2026-08-07 contract: Up iff mean(P,[T-w,T]) >= mean(P,[0,w]),
    w = 30s (5m markets) / 60s (15m). We accumulate the oracle tick stream
    into the strike average and the settlement average."""

    __slots__ = ("slug", "asset_id_up", "asset_id_dn", "t0_us", "t1_us",
                 "strike", "bids", "asks", "last_trade_px", "last_trade_us",
                 "book_us", "end_px", "w", "k_sum", "k_n", "r_sum", "r_n",
                 "last_oracle_px")

    def __init__(self, slug, asset_id_up, t0_us, t1_us, asset_id_dn=None):
        self.w = 30.0 if (t1_us - t0_us) <= 300_000_000 else 60.0
        self.k_sum = 0.0; self.k_n = 0        # strike window [t0, t0+w]
        self.r_sum = 0.0; self.r_n = 0        # settle window [t1-w, t1]
        self.last_oracle_px = None
        self.slug = slug
        self.asset_id_up = asset_id_up
        self.asset_id_dn = asset_id_dn
        self.end_px = None
        self.t0_us = t0_us
        self.t1_us = t1_us
        self.strike = None
        self.bids = []      # [(price, size)] best first
        self.asks = []
        self.last_trade_px = None
        self.last_trade_us = 0
        self.book_us = 0

    def on_oracle_tick(self, px, round_ts_us):
        """Accumulate the two averages that define the contract."""
        self.last_oracle_px = px
        if self.t0_us <= round_ts_us < self.t0_us + int(self.w * 1e6):
            self.k_sum += px; self.k_n += 1
        if self.t1_us - int(self.w * 1e6) <= round_ts_us < self.t1_us:
            self.r_sum += px; self.r_n += 1

    def strike_avg(self):
        """Known once w seconds of the window have elapsed."""
        return self.k_sum / self.k_n if self.k_n else None

    def settle_avg_so_far(self):
        return (self.r_sum, self.r_n)

    def best_bid(self):
        return self.bids[0] if self.bids else (None, 0.0)

    def best_ask(self):
        return self.asks[0] if self.asks else (None, 0.0)


class BotState:
    def __init__(self):
        self.binance_px = None
        self.binance_us = 0
        self.oracle_px = None
        self.oracle_round_ts_us = 0
        self.oracle_us = 0
        self.vol = OnlineVol(300.0)
        self.basis = deque(maxlen=600)   # (oracle/binance) samples, 1/s
        self.markets = {}                # slug -> MarketState
        self.pricer = GzPricer()

    # ---- feed handlers -------------------------------------------------
    def on_binance(self, price, ts_ms):
        self.binance_px = price
        self.binance_us = now_us()
        self.vol.update(ts_ms // 1000, price)
        if self.oracle_px and self.binance_px:
            self.basis.append(self.oracle_px / self.binance_px)

    def on_oracle(self, price, round_ts_ms):
        self.oracle_px = price
        self.oracle_round_ts_us = round_ts_ms * 1000
        self.oracle_us = now_us()
        for m in self.markets.values():
            m.on_oracle_tick(price, self.oracle_round_ts_us)
            if m.strike is None and self.oracle_round_ts_us >= m.t0_us:
                m.strike = price          # legacy field, kept for logging

    def on_book(self, asset_id, bids, asks, ts_ms):
        for m in self.markets.values():
            if m.asset_id_up == asset_id:
                m.bids = bids
                m.asks = asks
                m.book_us = now_us()
                return

    def on_trade(self, asset_id, price, ts_ms):
        for m in self.markets.values():
            if m.asset_id_up == asset_id:
                m.last_trade_px = price
                m.last_trade_us = now_us()
                return

    # ---- derived -------------------------------------------------------
    def spot_adj(self):
        """Basis-adjusted Binance spot (best live estimate of next oracle)."""
        if self.binance_px is None or len(self.basis) < 60:
            return None
        ratio = float(np.median(self.basis))
        return self.binance_px * ratio

    def fair(self, m: MarketState, t_us=None):
        """Fair value of the CURRENT (rolling-average) contract."""
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.dirname(
            _o.path.abspath(__file__))), "src"))
        from rollavg_pricer import fair as _fair
        t_us = t_us or now_us()
        T = (m.t1_us - m.t0_us) / 1e6
        t = (t_us - m.t0_us) / 1e6
        K = m.strike_avg()
        spot = self.spot_adj()
        if K is None or spot is None or t < m.w or t >= T:
            return None                      # strike not yet formed / expired
        if self.vol.var is None or self.vol.var <= 0:
            return None
        sigma = (self.vol.var ** 0.5) * spot     # dollar vol per sqrt(sec)
        r_sum, r_n = m.settle_avg_so_far()
        R = float(r_sum)                          # price-seconds (1 tick/sec)
        return float(_fair(spot, K, R, T, t, m.w, sigma))

    def fair_legacy(self, m: MarketState, t_us=None):
        """Pre-2026-08-07 pricer — kept ONLY to measure market adaptation."""
        t_us = t_us or now_us()
        rem_s = (m.t1_us - t_us) / 1e6
        spot = self.spot_adj()
        K = m.strike_avg() or m.strike
        if K is None or spot is None or rem_s <= 0:
            return None
        return self.pricer.fair(spot, K, self.vol.var, rem_s)

    def staleness(self):
        t = now_us()
        return {
            "binance_s": (t - self.binance_us) / 1e6 if self.binance_us else 1e9,
            "oracle_s": (t - self.oracle_us) / 1e6 if self.oracle_us else 1e9,
        }
