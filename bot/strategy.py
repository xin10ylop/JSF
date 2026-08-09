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
    """Post-2026-08-07 contract edge monitor / trader.

    The venue changed settlement on 2026-08-07 to
        Up iff mean(P,[T-w,T]) >= mean(P,[0,w]),  w=30s (5m) / 60s (15m).
    Any book still priced on the old terminal rule is systematically wrong
    in the back of the window: measured on real BTC paths, at 5s left the
    correct pricer scores Brier 0.0026 vs 0.0449 for the legacy pricer,
    with p90 disagreement of 22pp.

    This emits a signal when |correct_fair - market| clears `edge_min` in
    the final `window_s`. It ALWAYS logs the gap (even when not trading) so
    that paper mode measures whether the market has adapted to the new rule.
    """

    def __init__(self, cfg):
        self.window_s = cfg.get("window_s", 45)
        self.edge_min = cfg.get("edge_min", 0.08)
        self.size = cfg.get("size", 100)
        self.max_price = cfg.get("max_price", 0.97)
        self.observations = []

    def evaluate(self, state, m, t_us):
        rem = (m.t1_us - t_us) / 1e6
        if rem > self.window_s or rem <= 1.0:
            return None
        fv = state.fair(m, t_us)
        if fv is None:
            return None
        legacy = state.fair_legacy(m, t_us)
        bb, bbs = m.best_bid()
        ba, bas = m.best_ask()
        if bb is None or ba is None or not (0 < bb < ba < 1):
            return None
        mid = (bb + ba) / 2
        self.observations.append((m.slug, rem, mid, fv, legacy))
        # buy Up at ask when the correct pricer says the ask is cheap
        if fv - ba >= self.edge_min and ba <= self.max_price:
            return {"action": "taker_buy", "side": "Up", "px": ba,
                    "avail": bas, "size": self.size,
                    "reason": f"rollavg fv {fv:.3f} vs ask {ba:.3f} "
                              f"(legacy {legacy if legacy is None else round(legacy,3)}) rem {rem:.0f}s"}
        ask_dn = 1 - bb
        if (1 - fv) - ask_dn >= self.edge_min and ask_dn <= self.max_price:
            return {"action": "taker_buy", "side": "Down", "px": ask_dn,
                    "avail": bbs, "size": self.size,
                    "reason": f"rollavg fvD {1-fv:.3f} vs askD {ask_dn:.3f} rem {rem:.0f}s"}
        return None
