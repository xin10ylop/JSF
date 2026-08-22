"""Risk limits, halts and kill switch for the bot.

Hard rules:
- refuse to signal when any input is stale (oracle > 5s, book > 10s)
- per-market position cap (shares and $)
- max concurrent markets with exposure
- a LADDER of halts (see below) ending in a daily kill switch

Halt design. A halt exists to bound the loss when the EDGE ITSELF breaks
(the venue re-prices, a contract change, a corrupted feed) -- not to
punish ordinary variance. Every false trip costs the expected profit of
the hours it sits out, so the design goal is: trip fast on breakage,
almost never on noise.

Measured on the live record (630 markets, 27.8h): aggregate daily sd is
roughly $590 at current caps, mostly btc. The original $200/coin daily
stop was ~1 sd of a btc day -- it tripped on 2026-08-12 on what the band
breakdown shows was variance plus one soft band, and the bot then sat out
six profitable hours. That is insurance priced above the risk it covers.

The ladder, per coin process:

1. STREAK BREAKER (breakage detector). At avg fill px ~0.92 the record's
   hit rate is ~0.95, so losses arrive ~1.6 per 20 settled markets.
   `streak_losses` (6) losses inside the last `streak_n` (20) settled
   markets has probability ~4e-3 per window under the healthy record --
   but is hit almost immediately when the edge actually inverts. Trips a
   `cooloff_s` (2h) pause, not a day kill: a broken venue is usually still
   broken two hours later and the probe below will catch it, while a
   variance trip costs only two hours.
2. PROBE. After the cool-off the bot resumes at `resume_frac` (half) size
   for `probe_markets` (10) settled markets. `probe_losses` (3) losses in
   the probe (~2e-2 under the healthy record) = the breakage was real ->
   kill to end of day. A clean probe restores full size.
3. DAILY DOLLAR BACKSTOP. `daily_loss_limit` now sits at ~1.5-2 sd of a
   coin day ($400): with the streak breaker in front of it, the dollar
   stop is the insurance-of-last-resort against slow bleeds the streak
   test is blind to (many small losses under high hit rates).

State survives restarts: bot/run.py re-seeds day_pnl AND the streak from
today's settle lines, so a deploy cannot clear a tripped halt.
"""
import time
from collections import deque


def _now_us():
    return int(time.time() * 1_000_000)


class Risk:
    def __init__(self, cfg):
        self.max_market_shares = cfg.get("max_market_shares", 200)
        self.max_market_dollars = cfg.get("max_market_dollars", 150.0)
        # Separate dollar cap for CHEAP entries (px < cheap_px). The two
        # profitable bands need different sizes, and the global cap is
        # sized for the deep >=0.90 band -- but re-fires accumulate cost
        # until the cap binds (a $12 cap became a single -$12.70 loss at
        # 17c), so without its own bound the cheap lottery band would
        # quietly stack to the BIG cap: a 23%-hit-rate bet at locked-band
        # size. Defaults to the global cap, i.e. no behavior change
        # unless configured.
        self.max_market_dollars_cheap = cfg.get(
            "max_market_dollars_cheap", self.max_market_dollars)
        self.cheap_px = cfg.get("cheap_px", 0.90)
        self.max_concurrent = cfg.get("max_concurrent_markets", 4)
        self.daily_loss_limit = cfg.get("daily_loss_limit", 400.0)
        # stale_binance_s was dead config: loaded here, enforced nowhere
        # (inputs_ok deliberately dropped the Binance check; the actual
        # Binance freshness cutoff lives in state.spot_adj/_spot_fresh).
        # A knob that silently does nothing is worse than no knob.
        self.stale_oracle_s = cfg.get("stale_oracle_s", 5.0)
        self.stale_book_s = cfg.get("stale_book_s", 10.0)
        h = cfg.get("halt", {})
        self.streak_n = int(h.get("streak_n", 20))
        self.streak_losses = int(h.get("streak_losses", 6))
        self.cooloff_s = float(h.get("cooloff_s", 7200.0))
        self.resume_frac = float(h.get("resume_frac", 0.5))
        self.probe_markets = int(h.get("probe_markets", 10))
        self.probe_losses = int(h.get("probe_losses", 3))
        # UTC, explicitly. Markets, logs and the scorer all live in UTC;
        # localtime here would roll the daily loss limit at whatever
        # timezone the host happens to be in.
        self.day = time.strftime("%Y-%m-%d", time.gmtime())
        self.day_pnl = 0.0
        self.killed = False
        self.recent = deque(maxlen=max(self.streak_n, 50))
        self.halt_until_us = 0
        self.probe_left = 0
        self.probe_losses_seen = 0
        self.halts = 0

    # ---- state helpers --------------------------------------------------
    def state_str(self):
        if self.killed:
            return "KILLED"
        if _now_us() < self.halt_until_us:
            return f"COOLOFF({max(0, (self.halt_until_us - _now_us()) // 1_000_000)}s)"
        if self.probe_left > 0:
            return (f"PROBE({self.probe_left} mkts left, "
                    f"{self.probe_losses_seen} losses)")
        return "NORMAL"

    def in_cooloff(self):
        if _now_us() < self.halt_until_us:
            return True
        if self.halt_until_us:
            # cool-off just expired: arm the probe exactly once
            self.halt_until_us = 0
            self.probe_left = self.probe_markets
            self.probe_losses_seen = 0
        return False

    def _roll_day(self):
        d = time.strftime("%Y-%m-%d", time.gmtime())
        if d != self.day:
            # A DAILY loss limit must reset daily. Without clearing
            # `killed` the first bad day stops the bot permanently and, in
            # paper mode, silently ends the measurement run.
            self.day = d
            self.day_pnl = 0.0
            self.killed = False
            self.recent.clear()
            self.halt_until_us = 0
            self.probe_left = 0
            self.probe_losses_seen = 0

    # ---- events ---------------------------------------------------------
    def on_settle_pnl(self, pnl):
        """One call per settled market with a position. pnl sign IS the
        outcome for a long-only taker: won -> pnl > 0, lost -> pnl < 0."""
        self._roll_day()
        self.day_pnl += pnl
        if pnl == 0:
            return
        won = pnl > 0
        self.recent.append(won)
        # probe accounting first: a probe loss counts toward its own bar
        if self.probe_left > 0:
            self.probe_left -= 1
            if not won:
                self.probe_losses_seen += 1
            if self.probe_losses_seen >= self.probe_losses:
                self.killed = True          # breakage confirmed: out for
                self.probe_left = 0         # the rest of the UTC day
            # a clean probe (probe_left hits 0 first) simply resumes
        # streak breaker: losses inside the trailing window
        recent = list(self.recent)[-self.streak_n:]
        if (len(recent) >= self.streak_n
                and sum(1 for w in recent if not w) >= self.streak_losses
                and not self.killed
                and _now_us() >= self.halt_until_us):
            self.halt_until_us = _now_us() + int(self.cooloff_s * 1e6)
            self.halts += 1
            self.recent.clear()             # the next window starts fresh
        # dollar backstop, last
        if self.day_pnl < -abs(self.daily_loss_limit):
            self.killed = True

    def reconcile_day_pnl(self, day_pnl):
        """Overwrite today's P&L with the venue's own figure.

        day_pnl is an accumulator fed by settle events, so it only knows
        about markets that resolved while this process was alive. A
        restart across a settlement loses that P&L permanently -- live
        it read +0.28 against a real -12.15 after a session of restarts,
        with a -12.70 loss missing entirely. Since day_pnl is what the
        daily loss limit reads, drift in it disables the stop.

        bot/ledger.py recomputes the day from Polymarket's cash record,
        which restarts cannot erase; this makes that authoritative and
        re-evaluates the kill. Returns the drift it corrected.
        """
        self._roll_day()
        drift = day_pnl - self.day_pnl
        self.day_pnl = day_pnl
        if self.day_pnl < -abs(self.daily_loss_limit):
            self.killed = True
        return drift

    def seed(self, day_pnl, outcomes, logged=None):
        """Restart persistence: today's realized P&L and settle outcomes
        (won bools, oldest first) rebuilt from the decision log, plus the
        LAST logged risk-state transition if one exists.

        The transition line is authoritative when present: day_pnl alone
        cannot distinguish a probe-kill at -$180 from a healthy -$180 day,
        and re-inferring the streak from the day's outcomes re-armed a
        fresh full cool-off hours after the original had been served.
        Without a transition line (first deploy, rotated log) fall back to
        conservative inference from the outcomes.
        """
        self._roll_day()
        self.day_pnl = day_pnl
        for w in outcomes[-self.recent.maxlen:]:
            self.recent.append(bool(w))
        if self.day_pnl < -abs(self.daily_loss_limit):
            self.killed = True
        if logged is not None and logged.get("day") == self.day:
            self.killed = self.killed or bool(logged.get("killed"))
            hu = int(logged.get("halt_until_us") or 0)
            if self.killed:
                return
            if hu > _now_us():
                self.halt_until_us = hu          # honor the ORIGINAL clock
                self.halts += 1
            elif hu:
                # the cool-off elapsed while we were down: probe now
                self.probe_left = self.probe_markets
                self.probe_losses_seen = 0
            else:
                self.probe_left = int(logged.get("probe_left") or 0)
                self.probe_losses_seen = int(logged.get("probe_losses")
                                             or 0)
            return
        recent = list(self.recent)[-self.streak_n:]
        if (len(recent) >= self.streak_n
                and sum(1 for w in recent if not w) >= self.streak_losses
                and not self.killed):
            self.halt_until_us = _now_us() + int(self.cooloff_s * 1e6)
            self.halts += 1
            self.recent.clear()

    # ---- gates ----------------------------------------------------------
    def inputs_ok(self, staleness, book_age_s):
        """Binance is an ENHANCEMENT, not a requirement.

        The oracle is the settlement source and `spot_adj` falls back to it,
        so demanding a fresh Binance tick only threw away opportunities --
        91% of pre-strategy rejections were Binance staleness caused by a
        throttled public mirror, not by anything being wrong.
        """
        self._roll_day()
        return (not self.killed
                and not self.in_cooloff()
                and staleness["oracle_s"] < self.stale_oracle_s
                and book_age_s < self.stale_book_s)

    def size_ok(self, current_shares, current_dollars, add_shares, add_px,
                n_markets_with_pos):
        if self.killed or self.in_cooloff():
            return 0.0
        if self.probe_left > 0:
            add_shares = add_shares * self.resume_frac
        if n_markets_with_pos >= self.max_concurrent and current_shares == 0:
            return 0.0
        room_sh = self.max_market_shares - current_shares
        cap = self.max_market_dollars_cheap \
            if add_px < self.cheap_px else self.max_market_dollars
        # current_dollars is the market's TOTAL cost regardless of band;
        # a cheap entry therefore also counts locked spend against its
        # smaller cap. Within one 30s window that mix is rare, and the
        # error direction is refusal, not excess.
        room_usd = cap - current_dollars
        if room_sh <= 0 or room_usd <= 0:
            return 0.0
        return max(0.0, min(add_shares, room_sh, room_usd / max(add_px, 0.01)))
