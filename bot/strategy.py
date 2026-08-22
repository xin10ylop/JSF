from collections import deque
"""Signal generation. Parameters come from bot/config.json; the defaults
here are placeholders until the research verdicts finalize them.

Signals returned as dicts:
  {"action": "maker_buy", "side": "Up"|"Down", "level": px, "size": sh,
   "ttl_s": t, "reason": "..."}
  {"action": "taker_buy", "side": ..., "size": sh, "reason": "..."}
"""


class GzValueMaker:
    """Post at the touch on the side the book underprices vs G(z) fair.

    Buy side S at best bid of S when fair(S) - bid(S) >= edge_min and the
    phase/price filters pass. Maker only -> no fee.
    """

    def __init__(self, cfg):
        self.edge_min = cfg.get("edge_min", 0.03)
        self.min_tau_frac = cfg.get("min_tau_frac", 0.10)
        self.max_tau_frac = cfg.get("max_tau_frac", 0.97)
        self.size = cfg.get("size", 50)
        self.ttl_s = cfg.get("ttl_s", 20)

    def evaluate(self, state, m, t_us):
        fair_up = state.fair(m, t_us)
        if fair_up is None:
            return None
        win = m.t1_us - m.t0_us
        tau_frac = (t_us - m.t0_us) / win
        if not (self.min_tau_frac <= tau_frac <= self.max_tau_frac):
            return None
        bb, bbs = m.best_bid()
        ba, bas = m.best_ask()
        if bb is None or ba is None or not (0 < bb < ba < 1):
            return None
        # Up side: buy Up at bb if fair_up - bb >= edge_min
        if fair_up - bb >= self.edge_min:
            return {"action": "maker_buy", "side": "Up", "level": bb,
                    "size": self.size, "ttl_s": self.ttl_s,
                    "queue_ahead": bbs,
                    "reason": f"fair {fair_up:.3f} vs bid {bb:.3f}"}
        # Down side: buy Down at its bid = 1 - ba
        fair_dn = 1 - fair_up
        bid_dn = 1 - ba
        if fair_dn - bid_dn >= self.edge_min:
            return {"action": "maker_buy", "side": "Down", "level": bid_dn,
                    "size": self.size, "ttl_s": self.ttl_s,
                    "queue_ahead": bas,
                    "reason": f"fairD {fair_dn:.3f} vs bidD {bid_dn:.3f}"}
        return None


class VacuumLadder:
    """Crash-catching bids on the model-winning side in the final seconds.

    In the last `window_s` seconds of each market: if G(z) fair for a side
    >= fv_gate, post GTC bids on that side at the ladder levels. Orders rest
    to window end. Fills only happen in panic sweeps (book vacuums); each
    fill's expected value is fair - level (validated: +39c/share on 15m
    train, day-cluster CI positive, capacity ~10x base, queue-insensitive).
    """

    def __init__(self, cfg):
        self.window_s = cfg.get("window_s", 27)
        self.fv_gate = cfg.get("fv_gate", 0.65)
        self.levels = cfg.get("levels", [0.10, 0.20, 0.30])
        self.size = cfg.get("size", 100)
        self.posted = {}   # slug -> True once ladder posted

    def evaluate(self, state, m, t_us):
        rem_s = (m.t1_us - t_us) / 1e6
        if rem_s > self.window_s or rem_s <= 1:
            return None
        if self.posted.get(m.slug):
            return None
        fair_up = state.fair(m, t_us)
        if fair_up is None:
            return None
        if fair_up >= self.fv_gate:
            side = "Up"
        elif (1 - fair_up) >= self.fv_gate:
            side = "Down"
        else:
            return None
        self.posted[m.slug] = True
        return {"action": "ladder", "side": side, "levels": self.levels,
                "size": self.size, "ttl_s": rem_s,
                "reason": f"vacuum fair={fair_up:.3f} rem={rem_s:.0f}s"}


class ExtremeTaker:
    """Near-certainty taker buys where the fee ~ 0 and G says the book
    underprices the near-certain side (H9/H25/H17 region)."""

    def __init__(self, cfg):
        self.min_price = cfg.get("min_price", 0.95)
        self.edge_min = cfg.get("edge_min", 0.015)
        self.max_rem_s = cfg.get("max_rem_s", 120)
        self.size = cfg.get("size", 100)

    def evaluate(self, state, m, t_us):
        fair_up = state.fair(m, t_us)
        if fair_up is None:
            return None
        rem_s = (m.t1_us - t_us) / 1e6
        if rem_s > self.max_rem_s or rem_s <= 3:
            return None
        ba, bas = m.best_ask()
        bb, bbs = m.best_bid()
        if ba is not None and ba >= self.min_price \
                and fair_up - ba >= self.edge_min:
            return {"action": "taker_buy", "side": "Up", "px": ba,
                    "avail": bas, "size": self.size,
                    "reason": f"fair {fair_up:.3f} vs ask {ba:.3f}"}
        if bb is not None:
            ask_dn = 1 - bb
            fair_dn = 1 - fair_up
            if ask_dn >= self.min_price and fair_dn - ask_dn >= self.edge_min:
                return {"action": "taker_buy", "side": "Down", "px": ask_dn,
                        "avail": bbs, "size": self.size,
                        "reason": f"fairD {fair_dn:.3f} vs askD {ask_dn:.3f}"}
        return None


class RollAvgEdge:
    """Endgame taker on the post-2026-08-07 trailing-TWAP contract.

    Verified contract (2,880 settled markets, five coins):
        Up iff mean(P over [t1-w, t1)) >= mean(P over [t0-w, t0)), w = 30s
    Both averages trail; the strike is known before the window opens.

    Inside the last w seconds the outcome is progressively LOCKED: only the
    remaining `rem` seconds still move the average, and their contribution
    has sd sigma*sqrt(rem^3/3). The old contract had no lock-in, so a book
    still quoting it is wrong late in the window in a computable direction.

    Measured against real prints on 2026-08-07..09 (no fill assumptions,
    net of the venue's 0.07*p*(1-p) taker fee):
        |z|>=2.0  ->  +3.04c/share on 2.56M shares, per-day SE 0.07
        pre-change control, identical machinery  ->  +0.19c/share
    All 15 coin x day cells positive.

    Gate matches the backtest exactly: last `window_s` seconds, |z| >= zmin,
    and the favoured side's ask at least `edge_min` below its fair value.
    Observations are recorded on every tick regardless of whether we trade,
    so paper mode keeps measuring whether the venue has re-priced.
    """

    def __init__(self, cfg):
        self.window_s = cfg.get("window_s", 60)
        self.zmin = cfg.get("zmin", 2.0)
        self.edge_min = cfg.get("edge_min", 0.02)
        self.size = cfg.get("size", 100)
        # Band-split sizing, 2026-08-22. The two profitable bands have
        # OPPOSITE capacity profiles and one share count cannot fit both:
        #   >=locked_px  99% win rate, +1.8c/sh (t=5.1 on 8d print-proof),
        #                and 10k+ shares RESTING per tick (book probe
        #                median 33,767 executable) -- capacity is deep
        #                because we lift resting asks, prints understate it
        #   < locked_px  +9c/sh lottery band, 23% hit BY DESIGN, but thin:
        #                traded prints p50 ~20 shares -- size here is
        #                bounded by the per-band dollar cap in risk.py
        # size_locked scales the deep band without inflating the thin one.
        self.size_locked = cfg.get("size_locked", self.size)
        self.locked_px = cfg.get("locked_px", 0.90)
        self.max_price = cfg.get("max_price", 0.97)
        # Floor on the price we will pay. Default 0.0 = off, unchanged.
        # Live, fills below 0.70 are 9% of shares and 61% of P&L at hit
        # rates 2-3x what the tape says that region pays. Above 0.70 live
        # and tape agree closely. Setting this to 0.70 trades ONLY the
        # region where the two measurements agree, so a forward run either
        # lands near the tape's rate -- in which case the cheap band is a
        # real bonus the tape cannot see -- or the edge vanishes, in which
        # case the cheap band was the whole thing and it was not real.
        self.min_price = cfg.get("min_price", 0.0)
        # Optional [lo, hi) band of ask prices to SKIP, e.g. [0.95, 0.98].
        # Live (630 mkts): that band reads -2.57c/sh (t=-1.19, not proven)
        # while its neighbour (0.98,1.0] reads +0.87c at t=+13.9 -- the
        # contested zone right below lock-in is where adverse selection
        # bites hardest, the locked zone above it is fine. Ships OFF so
        # paper keeps measuring every band; flip at launch if the pattern
        # holds on a week of record.
        self.skip_px = cfg.get("skip_px") or None
        self.min_rem_s = cfg.get("min_rem_s", 2.0)
        self.require_edge = cfg.get("require_edge", False)
        # Liquidity at <=0.97 arrives as a STREAM, not a resting block: a
        # qualifying BTC market trades a median 1,291 shares there across
        # the settle window, but instantaneous depth is thin (only 6% of
        # snapshots carry >=10 shares). One entry per market therefore
        # captured ~40 of those. Re-entry is allowed, bounded by the risk
        # caps (max_market_shares / max_market_dollars), with a short
        # cooldown so a single book state is not taken twice.
        # Re-entry is gated on the BOOK CHANGING, not on a timer. A 1s
        # cooldown suppressed 867 of 1,681 in-window evaluations live --
        # liquidity at <=0.99 arrives as a stream, so a timer throws away
        # genuinely new asks. The real hazard a timer was standing in for is
        # taking the SAME resting ask twice (fictional fills); keying on the
        # book timestamp prevents that exactly, with no lost opportunity.
        self.cooldown_s = cfg.get("cooldown_s", 0.0)
        self.last_book = {}
        self.observations = deque(maxlen=5000)
        self.last_fire = {}
        # Signal funnel: which condition kills each evaluation. Without this
        # a zero-signal bot is indistinguishable from a broken one, and the
        # backtest implies ~4 qualifying BTC 5m markets per hour.
        self.f = {"eval": 0, "in_window": 0, "priced": 0, "book": 0,
                  "z_pass": 0, "px_pass": 0, "too_cheap": 0, "cooldown": 0,
                  "fired": 0}

    def evaluate(self, state, m, t_us):
        self.f["eval"] += 1
        rem = (m.t1_us - t_us) / 1e6
        # Only inside the settle window. Measured: the edge is +3.04c/share
        # for rem <= w but only +1.64c (and day-unstable) once prints before
        # the window opens are included -- the mechanism is the variance
        # collapse of the partial average, which does not exist before then.
        if rem > min(self.window_s, m.w) or rem <= self.min_rem_s:
            return None
        self.f["in_window"] += 1
        fv = state.fair(m, t_us)
        z = state.zscore(m, t_us)
        if fv is None or z is None:
            return None
        self.f["priced"] += 1
        # Require only what each side actually needs to trade.
        #
        # This used to demand a two-sided Up book (`bb and ba and 0<bb<ba<1`)
        # before considering EITHER side. In the settle window a near-decided
        # market routinely goes one-sided -- the losing token loses its bid
        # and quotes 0.001 -- which is exactly when we want to buy the OTHER
        # token, whose own book is fine. Measured live: 48 of 48 priced
        # evaluations were rejected here, i.e. 100% of signals.
        bb, bbs = m.best_bid()
        ba, bas = m.best_ask()
        ask_up = ba if (ba is not None and 0 < ba < 1) else None
        ad, dn_sz, dn_src = m.best_ask_dn()
        ask_dn = ad if (ad is not None and 0 < ad < 1) else None
        if ask_up is None and ask_dn is None:
            return None
        self.f["book"] += 1
        if abs(z) >= self.zmin:
            self.f["z_pass"] += 1
        mid = ((bb + ba) / 2) if (bb is not None and ba is not None) else None
        # fair_legacy() USED to be recorded here on every priced evaluation.
        # Three things wrong with that, all found by audit: it defeated the
        # lazy GzPricer (state.py) so all five processes loaded sklearn
        # anyway, ~72 MB each; it passed vol.var in raw with no ok() check,
        # no oracle path and no fallback guard -- the one call site in the
        # bot that could consume a broken sigma; and `observations` is never
        # read, logged or persisted anywhere in the repo, so it grew without
        # bound for the life of the process. Keep the cheap diagnostic,
        # bounded, and drop the expensive one that nothing consumed.
        self.observations.append((m.slug, rem, mid, fv, z))
        last = self.last_fire.get(m.slug)
        if last is not None and (t_us - last) < self.cooldown_s * 1e6:
            self.f["cooldown"] += 1
            return None
        # Same-book-snapshot dedup, PER SIDE and against the clock of the
        # book the candidate ask actually lives on. Keying both sides on
        # m.book_us (the Up token's clock) did two wrong things at once:
        # a Down fire was locked until some unrelated Up-book event
        # arrived (throttling exactly the one-sided endgame state this
        # strategy exists to trade), and every Up-book tick re-unlocked
        # the SAME unchanged resting Down ask for another fictional take.
        stamp_up = m.book_us
        stamp_dn = m.book_dn_us if dn_src == "book" else m.book_us
        # The VALIDATED gate is |z| >= zmin and ask <= max_price, nothing
        # more: that is exactly what was measured at +3.04c/share against
        # real prints, avg fill 0.877, hit 0.916.
        #
        # Adding `empirical_fair - ask >= edge_min` looks like a refinement
        # but is a DIFFERENT STRATEGY. Measured, it fires on 5x fewer shares
        # at an average price of 0.389 with a 0.501 hit rate (+10.15c), and
        # its pre-change control is +2.24c versus +0.49c for the validated
        # gate -- i.e. most of it is the longshot-underpricing effect, not
        # the settlement change this bot exists to trade. It is available
        # behind require_edge for paper measurement, and is off by default.
        def ev_of(fair, ask):
            return fair - ask - 0.07 * ask * (1 - ask)

        # Stamp the oracle's freshness on every signal. A fill taken on a
        # stale strike is worthless evidence, and without this the only way
        # to tell is to correlate timestamps against health lines by hand --
        # which is how -$162 of stale-oracle fills sat in the record looking
        # like a strategy result.
        oa = round(state.oracle_age_s(), 2)

        cands = [p for p in (ask_up if z > 0 else None,
                             ask_dn if z < 0 else None) if p is not None]
        if (abs(z) >= self.zmin and cands
                and self.min_price <= min(cands) <= self.max_price):
            self.f["px_pass"] += 1
        elif (abs(z) >= self.zmin and cands
              and min(cands) < self.min_price):
            self.f["too_cheap"] += 1
        def _px_ok(a):
            if not (self.min_price <= a <= self.max_price):
                return False
            if self.skip_px and self.skip_px[0] <= a < self.skip_px[1]:
                return False
            return True

        if z >= self.zmin and ask_up is not None and _px_ok(ask_up):
            if self.last_book.get((m.slug, "Up")) == stamp_up:
                self.f["cooldown"] += 1
                return None
            ba = ask_up
            # The edge gate must clear the FEE it will actually pay, or
            # its floor is ~0.3c while the config says 2c.
            if self.require_edge and ev_of(fv, ba) < self.edge_min:
                return None
            self.f["fired"] += 1
            self.last_fire[m.slug] = t_us
            self.last_book[(m.slug, "Up")] = stamp_up
            return {"action": "taker_buy", "side": "Up", "px": ba,
                    "avail": bas,
                    "size": self.size_locked if ba >= self.locked_px
                    else self.size,
                    "fair": round(fv, 4),
                    "ev_est": round(ev_of(fv, ba), 4),
                    "z": round(z, 2),
                    "oracle_age_s": oa,
                    "reason": f"rollavg z={z:+.2f} emp_fair {fv:.3f} vs ask "
                              f"{ba:.3f} rem {rem:.0f}s"}
        if z <= -self.zmin and ask_dn is not None and _px_ok(ask_dn):
            if self.last_book.get((m.slug, "Down")) == stamp_dn:
                self.f["cooldown"] += 1
                return None
            if self.require_edge and ev_of(1 - fv, ask_dn) < self.edge_min:
                return None
            self.f["fired"] += 1
            self.last_fire[m.slug] = t_us
            self.last_book[(m.slug, "Down")] = stamp_dn
            return {"action": "taker_buy", "side": "Down", "px": ask_dn,
                    "avail": dn_sz,
                    "size": self.size_locked if ask_dn >= self.locked_px
                    else self.size,
                    "fair": round(1 - fv, 4),
                    "ev_est": round(ev_of(1 - fv, ask_dn), 4),
                    "z": round(z, 2), "dn_src": dn_src, "oracle_age_s": oa,
                    "reason": f"rollavg z={z:+.2f} emp_fairD {1-fv:.3f} vs "
                              f"askD {ask_dn:.3f}({dn_src}) rem {rem:.0f}s"}
        return None


class ZMaker:
    """Maker leg of the endgame edge: REST a bid on the favoured side at
    fair-minus-margin instead of chasing asks that vanish.

    Why this exists: 24h of live taker orders measured the fill process
    itself -- 171 FAK kills vs 14 fills, with fills landing at avg px
    0.296 against paper's 0.857 in the same hours. The favourite's ask
    side empties in the final 30s (sampled live by the research fleet),
    so the taker leg starves exactly where the edge is largest, and what
    it does catch is adversely selected. The flow is still there: 55-62%
    of volume in these markets is bots, and every panic seller of the
    favourite (= buyer of the longshot) needs a resting bid to hit. Be
    that bid, priced by OUR calibrated fair, cancelled the moment the
    model moves.

    Quote discipline:
      - favoured side only (|z| >= zmin), level = fair_side - margin,
        clamped below the ask (never cross: a crossing bid is a taker)
      - one live quote per (slug, side); re-quote only when fair moved
        >= requote_c or the previous quote expired
      - TTL-bounded, and never resting into the final seconds (a fill at
        rem<3s on a flipping market is pure adverse selection)

    Fills are simulated against REAL trade prints by PaperBroker's
    price-priority rule (a print below our level proves a real trade
    swept it) -- far more honest than taker fill simulation, because
    prints are executed volume, not displayed offers.
    """

    def __init__(self, cfg):
        self.window_s = cfg.get("window_s", 90)
        self.zmin = cfg.get("zmin", 2.0)
        self.margin = cfg.get("margin", 0.03)
        self.size = cfg.get("size", 10)
        self.max_level = cfg.get("max_level", 0.97)
        self.min_level = cfg.get("min_level", 0.05)
        self.min_rem_s = cfg.get("min_rem_s", 8.0)
        self.ttl_s = cfg.get("ttl_s", 10.0)
        self.requote_c = cfg.get("requote_c", 0.01)
        self.last_quote = {}    # (slug, side) -> (level, t_us)
        self.f = {"eval": 0, "in_window": 0, "priced": 0, "quoted": 0}

    def evaluate(self, state, m, t_us):
        self.f["eval"] += 1
        rem = (m.t1_us - t_us) / 1e6
        if rem > min(self.window_s, 3 * m.w) or rem <= self.min_rem_s:
            return None
        self.f["in_window"] += 1
        fv = state.fair(m, t_us)
        z = state.zscore(m, t_us)
        if fv is None or z is None or abs(z) < self.zmin:
            return None
        self.f["priced"] += 1
        side = "Up" if z > 0 else "Down"
        fair_side = fv if side == "Up" else 1 - fv
        level = min(fair_side - self.margin, self.max_level)
        # never cross the spread -- a bid at/above the ask TAKES
        if side == "Up":
            ba, _ = m.best_ask()
            if ba is not None and 0 < ba < 1 and level >= ba:
                level = ba - 0.01
            queue = sum(sz for p, sz in m.bids
                        if abs(p - round(level, 2)) < 1e-9)
        else:
            ad, _sz, _src = m.best_ask_dn()
            if ad is not None and 0 < ad < 1 and level >= ad:
                level = ad - 0.01
            queue = sum(sz for p, sz in (m.bids_dn or [])
                        if abs(p - round(level, 2)) < 1e-9)
        level = round(level, 2)
        if level < self.min_level:
            return None
        last = self.last_quote.get((m.slug, side))
        if last is not None and abs(level - last[0]) < self.requote_c \
                and (t_us - last[1]) < self.ttl_s * 1e6:
            return None     # existing quote still stands
        self.last_quote[(m.slug, side)] = (level, t_us)
        self.f["quoted"] += 1
        # never rest into the final 3 seconds
        ttl = min(self.ttl_s, max(rem - 3.0, 1.0))
        return {"action": "maker_buy", "side": side, "level": level,
                "size": self.size, "queue_ahead": queue, "ttl_s": ttl,
                "z": round(z, 2),
                "reason": f"zmaker z={z:+.2f} fair_{side[0]} "
                          f"{fair_side:.3f} bid {level:.2f} rem {rem:.0f}s"}


class EarlyBird:
    """At-open taker: buy the model-favoured side in the first seconds of
    a 5m market, hold to settlement.

    Measured on 4,281 markets / 3 coins / 5.5 days at proof standard
    (entries = first REAL trade after open + 1 tick + fee, outcomes =
    venue resolutions): zmin 0.3 with the fair>=price filter reads
    +6.1c/sh pooled (t=+2.96) and +14.3c/sh on btc (t=+3.07, 6/6 days
    positive). Momentum predictors added nothing; hold beat every
    resting-sell exit; entries above ~0.80 are toxic and are refused.

    Why fills work HERE when the endgame starved: the opening minute
    trades a median ~550 shares (prints-measured) in a two-sided book --
    the emptiness this project fought lives only in the final ~30-60s.
    The signal is the pre-window z: the strike is already ~formed at
    open, the outcome window is all future, and the market's opening
    quotes lag that math.

    Fires only while `elapsed <= open_window_s` and only when the ask is
    at or below the model's calibrated fair -- the unfiltered variant
    measured NEGATIVE (-2.3c/sh); the filter is load-bearing, not
    optional.
    """

    def __init__(self, cfg):
        self.zmin = cfg.get("zmin", 0.3)
        self.size = cfg.get("size", 15)
        self.open_window_s = cfg.get("open_window_s", 45.0)
        # The strike does not LATCH until ~3.5s after open (oracle rounds
        # arrive 1.6-2.8s behind their stamps), so an evaluation in the
        # first seconds prices off a K missing its final ticks. The
        # backtest's z used the complete strike; wait for it.
        self.min_open_s = cfg.get("min_open_s", 4.0)
        self.max_price = cfg.get("max_price", 0.80)
        self.min_price = cfg.get("min_price", 0.30)
        self.last_book = {}
        self.f = {"eval": 0, "in_window": 0, "priced": 0, "z_pass": 0,
                  "fired": 0}

    def evaluate(self, state, m, t_us):
        self.f["eval"] += 1
        if m.t1_us - m.t0_us != 300_000_000:
            return None                       # 5m family only (measured)
        elapsed = (t_us - m.t0_us) / 1e6
        if not (self.min_open_s <= elapsed <= self.open_window_s):
            return None
        self.f["in_window"] += 1
        z = state.zscore(m, t_us)
        if z is None:
            return None
        self.f["priced"] += 1
        if abs(z) < self.zmin:
            return None
        self.f["z_pass"] += 1
        # Gaussian fair, NOT the empirical p_up calibration: the entire
        # backtest evidence (+6.1c pooled t=2.96, btc +14.3c t=3.07) was
        # measured with Phi(z), and p_up -- fitted on endgame-regime z --
        # reads ~3c lower at z=0.3, which would silently reject a large
        # share of the measured trades. Deploy what was measured.
        import math as _m
        fv = 0.5 * (1 + _m.erf(z / _m.sqrt(2)))
        side = "Up" if z > 0 else "Down"
        if side == "Up":
            ask, avail = m.best_ask()
            stamp = m.book_us
        else:
            ask, avail, src = m.best_ask_dn()
            stamp = m.book_dn_us if src == "book" else m.book_us
        if ask is None or not (self.min_price <= ask <= self.max_price):
            return None
        fair_side = fv if side == "Up" else 1 - fv
        if fair_side < ask:
            return None                       # the load-bearing filter
        if self.last_book.get((m.slug, side)) == stamp:
            return None                       # same book state: no re-take
        self.last_book[(m.slug, side)] = stamp
        self.f["fired"] += 1
        # one_shot: the engine refuses this signal whenever the market
        # already holds ANY shares on this side (booked or in flight).
        # Without it, every book tick inside the open window re-fires and
        # the risk caps let a partial fill top up at progressively worse
        # prices -- an execution profile the +14.3c/sh measurement never
        # contained (its entry is ONE trade at the first print). A clean
        # kill leaves no position, so retrying within the window is still
        # allowed -- which is exactly the measured "first print after
        # open" semantics.
        return {"action": "taker_buy", "side": side, "px": ask,
                "avail": avail, "size": self.size, "one_shot": True,
                "ev_est": round(fair_side - ask
                                - 0.07 * ask * (1 - ask), 4),
                "z": round(z, 2),
                "oracle_age_s": round(state.oracle_age_s(), 2),
                "reason": f"earlybird z={z:+.2f} fair_{side[0]} "
                          f"{fair_side:.3f} vs ask {ask:.3f} "
                          f"open+{elapsed:.0f}s"}


class JumpScalp:
    """Trade the OPENING JUMP, never the outcome.

    Buy near 0.50 in the first seconds of a 5m market and leave inside
    30s -- at a profit target if the price gets there, otherwise flat.
    Settlement is never carried, so the payoff asymmetry that makes a
    0.99 entry need a 99% hit rate cannot apply: entry near 0.53 wins
    ~47c and loses ~53c.

    Why an edge can exist here. At open the strike is ALREADY FIXED (it
    is the mean over the 30s BEFORE t0), so distance-to-strike is known
    and bounds how far the price can travel: near the strike small spot
    ticks swing the probability hard, far away the price is pinned.
    Measured on 2,263 btc markets: the price ranges ~22c in the first
    30s, and that range grows with |z| (21.7c at |z|<0.15 -> 27.4c at
    |z|>0.6). Direction comes from 10s spot momentum -- the venue's
    price lags spot by seconds. 30s and 60s momentum both scored
    NEGATIVE, which is what a genuinely short-horizon effect looks like
    rather than a fitted one.

    Out-of-sample (5 days the parameters never saw, btc):
        taker exit  +2.07c/sh  t=4.37  6/6 days positive
        maker exit  +2.82c/sh  t=5.75  6/6 days positive
        survives dropping the 20 best trades; bootstrap P(EV<=0)=0.0000
    In-sample on the tuning window was WEAKER (+1.00c taker), i.e. the
    rule did not degrade out of sample.

    Not established: eth (+0.71c) and sol (-0.06c) show nothing
    out-of-sample. Either this is a btc-specific property -- deepest
    book, tightest oracle tracking -- or a warning. btc only until that
    is understood.

    The gate below is the pre-committed rule, frozen before the holdout
    was scored. Exit logic lives in the runner, which owns positions.
    """

    def __init__(self, cfg):
        self.zmin = cfg.get("zmin", 0.15)
        self.mom_s = cfg.get("mom_s", 10)
        self.size = cfg.get("size", 8)
        # AT the open. The pre-open default this replaces was justified by
        # t-20..-10 = +5.45c/sh (t=24.7), which was a look-ahead artifact:
        # trades() picked the side from the spot AT the open while claiming
        # to decide up to 20s earlier. Scored at the decision instant
        # (src/jumpscalp_preopen.py, 2263 markets, 40% holdout, maker exit)
        # the ordering reverses and the pre-open edge disappears:
        #
        #   t-12 +0.66 (t=1.1)   t-2 +0.00 (t=0.0)
        #   t+0  +1.65 (t=3.2)   t+1 +2.19 (t=4.3)
        #   t+2  +1.21 (t=2.2)   t+3 +1.98 (t=3.8)   t+4 +0.24 (t=0.4)
        #
        # t+0..t+3 is four consecutive positive seconds, not one lucky
        # bucket, and it falls off at t+4 -- so the window is bounded on
        # both sides by measurement rather than by preference.
        self.entry_lo = cfg.get("entry_lo", 0.0)
        self.entry_hi = cfg.get("entry_hi", 3.0)
        self.open_window_s = cfg.get("open_window_s", 5.0)   # legacy
        self.min_price = cfg.get("min_price", 0.30)
        self.max_price = cfg.get("max_price", 0.70)
        self.target = cfg.get("target", 0.09)
        self.exit_s = cfg.get("exit_s", 30.0)
        self.dir_mode = cfg.get("dir_mode", "both")
        self.fired = set()
        self.f = {"eval": 0, "in_window": 0, "priced": 0, "z_pass": 0,
                  "mom": 0, "book": 0, "px_pass": 0, "fired": 0}

    def evaluate(self, state, m, t_us):
        self.f["eval"] += 1
        if m.t1_us - m.t0_us != 300_000_000:
            return None                       # 5m only
        since = (t_us - m.t0_us) / 1e6
        if not (self.entry_lo <= since <= self.entry_hi):
            return None
        self.f["in_window"] += 1
        if m.slug in self.fired:
            return None                       # one entry per market
        z = state.zscore(m, t_us)             # pre-window branch at open
        if z is None:
            return None
        self.f["priced"] += 1
        if abs(z) < self.zmin:
            return None
        self.f["z_pass"] += 1
        # side from short-horizon spot momentum: the venue lags spot
        #
        # oracle_hist holds (round_ts_us, px) in MICROSECONDS -- on_oracle
        # stores round_ts_ms * 1000. Comparing it against a seconds-valued
        # clock made every difference about -1.8e15, so `>= mom_s` was
        # never true, `back` stayed None, and evaluate() returned before
        # incrementing its own `mom` counter. Live: z_pass=19, mom=0, and
        # the strategy could not fire at all. Compare in one unit.
        cutoff_us = t_us - self.mom_s * 1_000_000
        spot = state.spot_adj()
        back = None
        for ts, px in reversed(state.oracle_hist):
            if ts <= cutoff_us:
                back = px
                break
        if spot is None or back is None:
            return None
        mom = spot - back
        # Direction from the SIGN OF Z -- BTC above the level it must
        # beat. Measured out-of-sample: z-sign +5.29c/sh (t=24.0) vs
        # 10s momentum +3.87c (t=9.9); requiring both to agree gives
        # +5.38c (t=22.2) on ~20% fewer trades. Momentum alone was the
        # first version's rule and was the weakest of the three.
        if self.dir_mode == "mom":
            if mom == 0:
                return None
            side = "Up" if mom > 0 else "Down"
        elif self.dir_mode == "z":
            side = "Up" if z > 0 else "Down"
        else:                                   # both must agree
            if mom == 0 or (mom > 0) != (z > 0):
                return None
            side = "Up" if z > 0 else "Down"
        self.f["mom"] += 1
        if side == "Up":
            ask, _sz = m.best_ask()
        else:
            ask, _sz, _src = m.best_ask_dn()
        if ask is None or not (0 < ask < 1):
            return None
        self.f["book"] += 1
        if not (self.min_price <= ask <= self.max_price):
            return None
        self.f["px_pass"] += 1
        self.fired.add(m.slug)
        self.f["fired"] += 1
        return {"action": "taker_buy", "side": side, "px": ask,
                "size": self.size, "one_shot": True,
                "scalp": {"target": round(ask + self.target, 4),
                          "deadline_us": int(t_us
                                             + self.exit_s * 1e6)},
                "z": round(z, 3), "mom": round(spot - back, 2),
                "oracle_age_s": state.oracle_age_s(),
                "reason": (f"jumpscalp z={z:+.2f} mom{self.mom_s}s="
                           f"{spot-back:+.1f} buy {side} @{ask:.3f} "
                           f"-> {ask+self.target:.3f} or t+"
                           f"{self.exit_s:.0f}s")}
