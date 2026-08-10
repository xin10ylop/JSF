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


# Plausibility band for the 1s log-return sd of a major crypto pair.
# Measured on BTC 1s klines (2026-08-08..09): sd 1.34e-5, and the rolling
# 1h sd spans 4.7e-6 (5th pct) to 2.2e-5 (95th). Anything outside this band
# by an order of magnitude is a broken estimator, not a quiet market, and
# must not be allowed to inflate z.
VOL_SD_MIN = 1e-6
VOL_SD_MAX = 5e-4


class OnlineVol:
    """One-hour TRAILING standard deviation of 1s log returns.

    This must be the SAME estimator the edge was measured with. The
    backtest computes sigma as

        log(px).diff().rolling(3600, min_periods=600).std() * spot

    on Binance 1s klines. An earlier version of this class used a 300s
    halflife EWMA instead. That is a different estimator, and live it read
    2.6e-6 against a two-day actual of 1.34e-5 -- 5x low, which inflates z
    5x and makes the bot fire on noise. Sigma sits in the DENOMINATOR of z,
    so under-estimating it is the dangerous direction.

    So: keep the last 3600 one-second returns and take their sample sd
    (ddof=1, matching pandas). `seed()` fills the window from recent klines
    so a restart is correct from the first tick rather than after an hour.
    """

    WINDOW = 3600
    MIN_OBS = 600

    def __init__(self, window_s=WINDOW):
        self.window = int(window_s)
        self.rets = deque(maxlen=self.window)
        self.last_sec = None
        self.last_px = None
        self._override = None

    def force_var(self, v):
        """Pin the variance. Reconciliation only -- never call in live use."""
        self._override = float(v) if v is not None else None

    @property
    def var(self):
        if self._override is not None:
            return self._override
        n = len(self.rets)
        if n < self.MIN_OBS:
            return None
        m = sum(self.rets) / n
        return sum((r - m) ** 2 for r in self.rets) / (n - 1)

    def seed(self, returns):
        """Warm start from a sequence of 1s log returns."""
        for r in returns:
            if math.isfinite(r):
                self.rets.append(float(r))
        return self.var

    def ok(self):
        """True when the estimate is inside the plausibility band."""
        v = self.var
        return v is not None and v > 0 and VOL_SD_MIN <= math.sqrt(v) <= VOL_SD_MAX

    def update(self, sec, px):
        """Append one 1s log return per elapsed second boundary."""
        if self.last_sec is None or self.last_px is None or px <= 0:
            self.last_sec, self.last_px = sec, px
            return
        if sec <= self.last_sec:
            self.last_px = px          # track latest price within the second
            return
        dt = sec - self.last_sec
        r = math.log(px / self.last_px)
        if dt > 1:
            # spread a multi-second gap over its seconds so the window still
            # represents one return per second
            r /= math.sqrt(dt)
            for _ in range(min(int(dt), 10)):
                self.rets.append(r)
        else:
            self.rets.append(r)
        self.last_sec, self.last_px = sec, px


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
    """Post-2026-08-07 contract, as VERIFIED on 2,880 settled markets:

        Up  iff  mean(P over [t1-w, t1))  >=  mean(P over [t0-w, t0))

    BOTH averages are trailing (a Chainlink `*-usd-twap-30s-streams` feed
    sampled at the two boundaries), w = 30s. The strike therefore closes
    BEFORE the window opens and is a known constant for the whole life of
    the market -- there is no dead zone at the start.

    Both averages are read on demand from BotState's oracle ring buffer
    rather than accumulated here, because the strike window has already
    elapsed by the time the market is discovered.
    """

    __slots__ = ("slug", "asset_id_up", "asset_id_dn", "t0_us", "t1_us",
                 "strike", "bids", "asks", "last_trade_px", "last_trade_us",
                 "book_us", "end_px", "w", "k_fixed", "last_oracle_px",
                 "bids_dn", "asks_dn", "book_dn_us")

    def __init__(self, slug, asset_id_up, t0_us, t1_us, asset_id_dn=None):
        self.w = 30.0 if (t1_us - t0_us) <= 300_000_000 else 60.0
        self.k_fixed = None                   # strike, latched once at t0
        self.last_oracle_px = None
        self.slug = slug
        self.asset_id_up = asset_id_up
        self.asset_id_dn = asset_id_dn
        self.end_px = None
        self.t0_us = t0_us
        self.t1_us = t1_us
        self.strike = None
        self.bids = []      # Up token, [(price, size)] best first
        self.asks = []
        # The Down token has its OWN book. On Polymarket you take the Down
        # side by BUYING the Down token off its own ask; 1 - best_bid_up is
        # the price for SELLING Up, which needs Up inventory, and the two
        # books can diverge. Track both.
        self.bids_dn = []
        self.asks_dn = []
        self.book_dn_us = 0
        self.last_trade_px = None
        self.last_trade_us = 0
        self.book_us = 0

    def on_oracle_tick(self, px, round_ts_us):
        self.last_oracle_px = px

    def best_bid(self):
        return self.bids[0] if self.bids else (None, 0.0)

    def best_ask(self):
        return self.asks[0] if self.asks else (None, 0.0)

    def best_ask_dn(self):
        """Real Down ask, falling back to the Up-book mirror if absent."""
        if self.asks_dn:
            return self.asks_dn[0] + ("book",)
        bb, bbs = self.best_bid()
        if bb is None:
            return (None, 0.0, "none")
        return (round(1.0 - bb, 4), bbs, "mirror")


class BotState:
    def __init__(self):
        self.binance_px = None
        self.binance_us = 0
        self.oracle_px = None
        self.oracle_round_ts_us = 0
        self.oracle_us = 0
        self.vol = OnlineVol()
        self.basis = deque(maxlen=600)   # (oracle/binance) samples, 1/s
        self.markets = {}                # slug -> MarketState
        self.pricer = GzPricer()
        # oracle ring buffer: (round_ts_us, px). The strike window of a 5m
        # market has already elapsed when the market is discovered, so both
        # contract averages are read from here, not accumulated per market.
        self.oracle_hist = deque(maxlen=4000)   # ~1/s -> >1h of history
        self.backfill_oracle()
        self.seed_vol()

    def seed_vol(self, symbol="BTCUSDT", bars=3600):
        """Warm-start the vol estimator from recent public 1s klines.

        Free and unauthenticated. The endpoint caps at 1000 bars per call,
        so this pages backwards until the full one-hour window is filled --
        the window has to match the backtest's rolling(3600), not merely be
        the right order of magnitude. Live updates take over from the first
        tick and roll the oldest returns off the back.
        """
        try:
            import requests
            end = None
            chunks = []
            while sum(len(c) for c in chunks) < bars:
                params = {"symbol": symbol, "interval": "1s", "limit": 1000}
                if end is not None:
                    params["endTime"] = end
                r = requests.get(
                    "https://data-api.binance.vision/api/v3/klines",
                    params=params, headers={"User-Agent": "Mozilla/5.0"},
                    timeout=20)
                if r.status_code != 200:
                    break
                js = r.json()
                if not js:
                    break
                chunks.append([float(x[4]) for x in js])
                end = int(js[0][0]) - 1
            if not chunks:
                return None
            c = np.array([px for ch in reversed(chunks) for px in ch])
            if len(c) < OnlineVol.MIN_OBS:
                return None
            return self.vol.seed(np.diff(np.log(c)))
        except Exception:  # noqa: BLE001
            return None

    def backfill_oracle(self, symbol="btc/usd", lookback_s=1800):
        """Seed the oracle ring buffer from the recorder's RTDS log.

        The strike is mean(P over [t0-w, t0)), i.e. history from BEFORE a
        market opens. A freshly restarted bot has none, so it would sit
        blind for a full window. The recorder writes every oracle tick to
        data/live/rtds/*.jsonl, so a restart can pick up where it left off.
        """
        import glob
        import json as _j
        cutoff_us = (now_us() - lookback_s * 1_000_000)
        rows = []
        for f in sorted(glob.glob("data/live/rtds/*.jsonl"))[-3:]:
            try:
                with open(f) as fh:
                    for line in fh:
                        if symbol not in line:
                            continue
                        try:
                            d = _j.loads(line)
                        except Exception:  # noqa: BLE001
                            continue
                        p = d.get("payload", {})
                        if p.get("symbol") != symbol:
                            continue
                        ts_us = int(p["timestamp"]) * 1000
                        if ts_us >= cutoff_us:
                            rows.append((ts_us, float(p["value"])))
            except OSError:
                continue
        rows.sort()
        seen = set()
        for ts, px in rows:
            if ts in seen:
                continue
            seen.add(ts)
            self.oracle_hist.append((ts, px))
        return len(self.oracle_hist)

    def oracle_sigma_rel(self, lookback_s=3600):
        """1s log-return sd of the ORACLE series itself, gap-corrected.

        The backtest was self-consistent: path, both window averages and
        sigma all came from Binance 1s klines. The bot is not — it takes
        the two averages from the Chainlink feed (correctly, that is what
        settles) but sigma from Binance. Measured on recorded ticks the
        oracle is ~1.23x as volatile as Binance at 1s, so borrowing
        Binance's sigma understates the denominator of z by ~23% and makes
        the bot overconfident.

        Only adjacent-second pairs are used: the feed misses ~38% of
        seconds, and treating a 3-second move as a 1-second return would
        inflate sd by sqrt(3).
        """
        cutoff = now_us() - lookback_s * 1_000_000
        by_sec = {}
        for ts, px in self.oracle_hist:
            if ts >= cutoff and px > 0:
                by_sec[ts // 1_000_000] = px
        if len(by_sec) < 300:
            return None
        rets = []
        for sec, px in by_sec.items():
            prev = by_sec.get(sec - 1)
            if prev:
                rets.append(math.log(px / prev))
        if len(rets) < 300:
            return None
        m = sum(rets) / len(rets)
        var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
        sd = math.sqrt(var)
        return sd if VOL_SD_MIN <= sd <= VOL_SD_MAX else None

    def sigma_rel(self):
        """Relative 1s vol to price z with: oracle first, Binance fallback."""
        s = self.oracle_sigma_rel()
        if s is not None:
            return s
        return math.sqrt(self.vol.var) if self.vol.ok() else None

    def _integral(self, a_us, b_us):
        """Time-weighted integral of the oracle price over [a_us, b_us).

        Returns (price_seconds, seconds, n_ticks).

        This MUST be an integral, not a sum of ticks. The backtest works on
        Binance 1s klines, where exactly one sample covers each second, so
        summing samples IS the integral. The Chainlink RTDS feed does not
        tick at exactly 1 Hz, so summing its ticks scales the locked partial
        average by the tick rate and blows up z (observed live: z = -5322
        with 23s left). Holding each price until the next tick reproduces
        the backtest quantity regardless of feed rate.
        """
        if b_us <= a_us:
            return 0.0, 0.0, 0, False
        last_before = None
        pts = []
        for ts, px in self.oracle_hist:
            if ts <= a_us:
                last_before = px
            elif ts < b_us:
                pts.append((ts, px))
        if last_before is None and not pts:
            return 0.0, 0.0, 0, False
        covered = last_before is not None
        cur_t = a_us
        cur_px = last_before if last_before is not None else pts[0][1]
        total = 0.0
        for ts, px in pts:
            total += cur_px * (ts - cur_t)
            cur_t, cur_px = ts, px
        total += cur_px * (b_us - cur_t)
        return total / 1e6, (b_us - a_us) / 1e6, len(pts), covered

    def _avg_over(self, a_us, b_us):
        """Back-compat shim: (price_seconds, seconds) over the interval."""
        ps, secs, _, _ = self._integral(a_us, b_us)
        return ps, secs

    def oracle_rate(self, lookback_s=300):
        """Oracle ticks per second over the recent past (health signal)."""
        cutoff = now_us() - lookback_s * 1_000_000
        n = sum(1 for ts, _ in self.oracle_hist if ts >= cutoff)
        return round(n / lookback_s, 3)

    def strike_avg(self, m):
        """mean(P over [t0-w, t0)) -- latched once, then reused."""
        if m.k_fixed is not None:
            return m.k_fixed
        w_us = int(m.w * 1e6)
        ps, secs, nticks, covered = self._integral(m.t0_us - w_us, m.t0_us)
        # The average is a TIME-WEIGHTED integral, so it does not need many
        # ticks -- it needs a price in force from the start of the window
        # (`covered`) plus enough updates that it is not one stale quote
        # held across the whole thing. An earlier threshold of 0.3*w ticks
        # (9 for a 5m market) exceeded the oracle's actual rate of ~0.2-1.0
        # ticks/s, so K silently became None and NOTHING in the settle
        # window could be priced -- the bot logged zero signals with no
        # error anywhere.
        if secs <= 0 or not covered or nticks < 3:
            return None
        if now_us() >= m.t0_us:
            m.k_fixed = ps / secs            # window closed: latch it
        return ps / secs

    def settle_sum_so_far(self, m, t_us=None):
        """(price_seconds, seconds) already accumulated inside [t1-w, t1)."""
        t_us = t_us or now_us()
        w_us = int(m.w * 1e6)
        ps, secs, _, _ = self._integral(m.t1_us - w_us, min(t_us, m.t1_us))
        return ps, secs

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
        if (not self.oracle_hist
                or self.oracle_hist[-1][0] != self.oracle_round_ts_us):
            self.oracle_hist.append((self.oracle_round_ts_us, price))
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
            if m.asset_id_dn == asset_id:
                m.bids_dn = bids
                m.asks_dn = asks
                m.book_dn_us = now_us()
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
        """P(Up) for the CURRENT (trailing-TWAP) contract.

        Strike is backward-looking, so this is valid from t=0 onward.
        """
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.dirname(
            _o.path.abspath(__file__))), "src"))
        from rollavg_pricer import fair as _fair
        t_us = t_us or now_us()
        T = (m.t1_us - m.t0_us) / 1e6
        t = (t_us - m.t0_us) / 1e6
        K = self.strike_avg(m)
        spot = self.spot_adj()
        if K is None or spot is None or t < 0 or t >= T:
            return None
        srel = self.sigma_rel()
        if srel is None:
            return None                      # implausible vol -> do not price
        sigma = srel * spot                  # dollar vol per sqrt(sec)
        r_sum, _ = self.settle_sum_so_far(m, t_us)
        R = float(r_sum)                          # price-seconds
        gauss = float(_fair(spot, K, R, T, t, m.w, sigma))
        # Phi(z) is measurably overconfident here (z>3 settles Up 91.6%, not
        # 99.87%). Report the empirical calibration so edge_min binds on a
        # real number. See bot/calib.py.
        from bot.calib import p_up
        z = self.zscore(m, t_us)
        emp = p_up(z)
        return gauss if emp is None else float(emp)

    def zscore(self, m: MarketState, t_us=None):
        """Signed margin in sds of the still-unrealised part of the average.

        Inside the settle window this is the exact quantity the endgame
        backtest gated on; outside it, the phase-2 equivalent.
        """
        t_us = t_us or now_us()
        T = (m.t1_us - m.t0_us) / 1e6
        t = (t_us - m.t0_us) / 1e6
        K = self.strike_avg(m)
        spot = self.spot_adj()
        if K is None or spot is None or t >= T:
            return None
        srel = self.sigma_rel()
        if srel is None:
            return None
        sigma = srel * spot
        rem = T - t
        if t <= T - m.w:
            s = max((T - m.w) - t, 0.0)
            sd = sigma * ((s + m.w / 3.0) ** 0.5)
            return (spot - K) / sd if sd > 0 else None
        r_sum, _ = self.settle_sum_so_far(m, t_us)
        sd = sigma * ((rem ** 3) / 3.0) ** 0.5
        return (r_sum + spot * rem - K * m.w) / sd if sd > 0 else None

    def fair_legacy(self, m: MarketState, t_us=None):
        """Pre-2026-08-07 pricer — kept ONLY to measure market adaptation."""
        t_us = t_us or now_us()
        rem_s = (m.t1_us - t_us) / 1e6
        spot = self.spot_adj()
        K = self.strike_avg(m) or m.strike
        if K is None or spot is None or rem_s <= 0:
            return None
        return self.pricer.fair(spot, K, self.vol.var, rem_s)

    def staleness(self):
        t = now_us()
        return {
            "binance_s": (t - self.binance_us) / 1e6 if self.binance_us else 1e9,
            "oracle_s": (t - self.oracle_us) / 1e6 if self.oracle_us else 1e9,
        }
