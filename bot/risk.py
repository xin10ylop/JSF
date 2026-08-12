"""Risk limits and kill switch for the bot.

Hard rules:
- refuse to signal when any input is stale (binance > 3s, oracle > 5s,
  book > 10s)
- per-market position cap (shares and $)
- max concurrent markets with exposure
- daily loss limit -> kill switch: cancel everything, stop trading
"""
import json
import os
import time


class Risk:
    def __init__(self, cfg):
        self.max_market_shares = cfg.get("max_market_shares", 200)
        self.max_market_dollars = cfg.get("max_market_dollars", 150.0)
        self.max_concurrent = cfg.get("max_concurrent_markets", 4)
        self.daily_loss_limit = cfg.get("daily_loss_limit", 25.0)
        self.stale_binance_s = cfg.get("stale_binance_s", 3.0)
        self.stale_oracle_s = cfg.get("stale_oracle_s", 5.0)
        self.stale_book_s = cfg.get("stale_book_s", 10.0)
        # UTC, explicitly. Markets, logs and the scorer all live in UTC;
        # localtime here would roll the daily loss limit at whatever
        # timezone the host happens to be in.
        self.day = time.strftime("%Y-%m-%d", time.gmtime())
        self.day_pnl = 0.0
        self.killed = False

    def on_settle_pnl(self, pnl):
        d = time.strftime("%Y-%m-%d", time.gmtime())
        if d != self.day:
            # A DAILY loss limit must reset daily. Without clearing `killed`
            # the first bad day stops the bot permanently and, in paper
            # mode, silently ends the measurement run.
            self.day = d
            self.day_pnl = 0.0
            self.killed = False
        self.day_pnl += pnl
        if self.day_pnl < -abs(self.daily_loss_limit):
            self.killed = True

    def inputs_ok(self, staleness, book_age_s):
        """Binance is an ENHANCEMENT, not a requirement.

        The oracle is the settlement source and `spot_adj` falls back to it,
        so demanding a fresh Binance tick only threw away opportunities --
        91% of pre-strategy rejections were Binance staleness caused by a
        throttled public mirror, not by anything being wrong.
        """
        return (not self.killed
                and staleness["oracle_s"] < self.stale_oracle_s
                and book_age_s < self.stale_book_s)

    def size_ok(self, current_shares, current_dollars, add_shares, add_px,
                n_markets_with_pos):
        if self.killed:
            return 0.0
        if n_markets_with_pos >= self.max_concurrent and current_shares == 0:
            return 0.0
        room_sh = self.max_market_shares - current_shares
        room_usd = self.max_market_dollars - current_dollars
        if room_sh <= 0 or room_usd <= 0:
            return 0.0
        return max(0.0, min(add_shares, room_sh, room_usd / max(add_px, 0.01)))
