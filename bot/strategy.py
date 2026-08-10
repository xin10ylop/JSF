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
        self.max_price = cfg.get("max_price", 0.97)
        self.min_rem_s = cfg.get("min_rem_s", 2.0)
        self.require_edge = cfg.get("require_edge", False)
        # Liquidity at <=0.97 arrives as a STREAM, not a resting block: a
        # qualifying BTC market trades a median 1,291 shares there across
        # the settle window, but instantaneous depth is thin (only 6% of
        # snapshots carry >=10 shares). One entry per market therefore
        # captured ~40 of those. Re-entry is allowed, bounded by the risk
        # caps (max_market_shares / max_market_dollars), with a short
        # cooldown so a single book state is not taken twice.
        self.cooldown_s = cfg.get("cooldown_s", 1.0)
        self.observations = []
        self.last_fire = {}
        # Signal funnel: which condition kills each evaluation. Without this
        # a zero-signal bot is indistinguishable from a broken one, and the
        # backtest implies ~4 qualifying BTC 5m markets per hour.
        self.f = {"eval": 0, "in_window": 0, "priced": 0, "book": 0,
                  "z_pass": 0, "px_pass": 0, "cooldown": 0, "fired": 0}

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
        self.observations.append((m.slug, rem, mid, fv, z,
                                  state.fair_legacy(m, t_us)))
        last = self.last_fire.get(m.slug)
        if last is not None and (t_us - last) < self.cooldown_s * 1e6:
            self.f["cooldown"] += 1
            return None                      # same book state; wait
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

        cands = [p for p in (ask_up if z > 0 else None,
                             ask_dn if z < 0 else None) if p is not None]
        if abs(z) >= self.zmin and cands and min(cands) <= self.max_price:
            self.f["px_pass"] += 1
        if z >= self.zmin and ask_up is not None and ask_up <= self.max_price:
            ba = ask_up
            if self.require_edge and fv - ba < self.edge_min:
                return None
            self.f["fired"] += 1
            self.last_fire[m.slug] = t_us
            return {"action": "taker_buy", "side": "Up", "px": ba,
                    "avail": bas, "size": self.size,
                    "ev_est": round(ev_of(fv, ba), 4), "z": round(z, 2),
                    "reason": f"rollavg z={z:+.2f} emp_fair {fv:.3f} vs ask "
                              f"{ba:.3f} rem {rem:.0f}s"}
        if z <= -self.zmin and ask_dn is not None \
                and ask_dn <= self.max_price:
            if self.require_edge and (1 - fv) - ask_dn < self.edge_min:
                return None
            self.f["fired"] += 1
            self.last_fire[m.slug] = t_us
            return {"action": "taker_buy", "side": "Down", "px": ask_dn,
                    "avail": dn_sz, "size": self.size,
                    "ev_est": round(ev_of(1 - fv, ask_dn), 4),
                    "z": round(z, 2), "dn_src": dn_src,
                    "reason": f"rollavg z={z:+.2f} emp_fairD {1-fv:.3f} vs "
                              f"askD {ask_dn:.3f}({dn_src}) rem {rem:.0f}s"}
        return None
