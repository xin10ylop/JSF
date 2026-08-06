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
        if self.last_px is not None and self.last_sec is not None:
            dt = sec - self.last_sec
            if dt > 0:
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
    def __init__(self, path="data/gz_models.pkl", k_cal=None):
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
    __slots__ = ("slug", "asset_id_up", "t0_us", "t1_us", "strike",
                 "bids", "asks", "last_trade_px", "last_trade_us",
                 "book_us")

    def __init__(self, slug, asset_id_up, t0_us, t1_us):
        self.slug = slug
        self.asset_id_up = asset_id_up
        self.t0_us = t0_us
        self.t1_us = t1_us
        self.strike = None
        self.bids = []      # [(price, size)] best first
        self.asks = []
        self.last_trade_px = None
        self.last_trade_us = 0
        self.book_us = 0

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
        # capture strikes for any market whose t0 this round crosses
        for m in self.markets.values():
            if m.strike is None and self.oracle_round_ts_us >= m.t0_us:
                m.strike = price

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
        t_us = t_us or now_us()
        rem_s = (m.t1_us - t_us) / 1e6
        spot = self.spot_adj()
        if m.strike is None or spot is None or rem_s <= 0:
            return None
        return self.pricer.fair(spot, m.strike, self.vol.var, rem_s)

    def staleness(self):
        t = now_us()
        return {
            "binance_s": (t - self.binance_us) / 1e6 if self.binance_us else 1e9,
            "oracle_s": (t - self.oracle_us) / 1e6 if self.oracle_us else 1e9,
        }
