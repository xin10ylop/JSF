"""Paper broker: simulates maker/taker orders against the live feed with the
same conservative fill rules as the backtest.

Maker BUY at level L, size Q, posted at t:
- a later trade print at price < L on the same asset fills the remainder
  (price priority proves the level was swept),
- a print exactly at L fills only beyond the displayed queue captured at
  post time,
- cancel stops all fills.

Taker BUY: fills at the current best ask up to its displayed size, pays the
0.07*p*(1-p) fee. Never assumes more size than displayed.

All orders, fills, and settlements are logged as jsonl for reconciliation
against the backtest.
"""
import json
import os
import time


def now_us():
    return int(time.time() * 1_000_000)


class PaperBroker:
    def __init__(self, log_path="logs/paper_fills.jsonl"):
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        self.log = open(log_path, "a")
        self.orders = []          # open maker orders
        self.positions = {}       # slug -> {"shares": s, "cost": c}
        self.next_id = 1

    def _emit(self, kind, **kw):
        kw["kind"] = kind
        kw["t_us"] = now_us()
        self.log.write(json.dumps(kw, separators=(",", ":")) + "\n")
        self.log.flush()

    # ---- order entry ---------------------------------------------------
    def maker_buy(self, slug, asset_id, side_label, level, size,
                  queue_ahead, expire_us, meta=None):
        oid = self.next_id
        self.next_id += 1
        self.orders.append(dict(
            oid=oid, slug=slug, asset_id=asset_id, side=side_label,
            level=level, size=size, remaining=size, queue=queue_ahead,
            expire_us=expire_us))
        self._emit("order", oid=oid, slug=slug, side=side_label, level=level,
                   size=size, queue=queue_ahead, expire_us=expire_us,
                   meta=meta or {})
        return oid

    def taker_fills(self, slug, side_label, legs, meta=None, fee_per_sh=None):
        """Book one taker execution made of one or more price legs.

        `legs` is [(price, shares)] as produced by the caller walking the
        book. Each leg is logged separately so the scorer sees the real
        prices paid, not a flattering touch price. Returns total shares.

        `fee_per_sh` overrides the modelled taker fee; None (the default)
        books 0.07*px*(1-px). Real venue fills book the default too: the
        venue charges that fee ON TOP of the matched amount in collateral
        (verified to the cent against account activity on the first live
        fills), so the modelled fee IS the real one.
        """
        tot = 0.0
        for px, sh in legs:
            if sh <= 0 or px is None:
                continue
            fee = fee_per_sh if fee_per_sh is not None \
                else 0.07 * px * (1 - px)
            pos = self.positions.setdefault(
                (slug, side_label), {"shares": 0.0, "cost": 0.0})
            pos["shares"] += sh
            pos["cost"] += sh * (px + fee)
            self._emit("taker_fill", slug=slug, side=side_label, px=px,
                       shares=sh, fee_per_sh=fee, meta=meta or {})
            tot += sh
        return tot

    def taker_buy(self, slug, side_label, ask_px, ask_sz, size, meta=None):
        """Single-level convenience wrapper (kept for the tests)."""
        return self.taker_fills(slug, side_label,
                                [(ask_px, min(size, ask_sz))], meta)

    def cancel_all(self, slug=None):
        for o in self.orders:
            if slug is None or o["slug"] == slug:
                o["remaining"] = 0
        self.orders = [o for o in self.orders if o["remaining"] > 0]

    # ---- feed-driven fill simulation ----------------------------------
    def on_trade_print(self, asset_id, price, size, t_us):
        """Trade prints drive maker fills. Price is in Up-token terms; an
        order on side 'Down' at level L maps to Up-print at 1-L."""
        for o in self.orders:
            if o["remaining"] <= 0 or t_us > o["expire_us"]:
                continue
            if o["asset_id"] != asset_id:
                continue
            lvl = o["level"] if o["side"] == "Up" else 1 - o["level"]
            hit = price < lvl - 1e-9 if o["side"] == "Up" \
                else price > lvl + 1e-9
            at = abs(price - lvl) <= 1e-9
            fill = 0.0
            if hit:
                fill = min(o["remaining"], size)
            elif at:
                beyond = max(0.0, size - o["queue"])
                o["queue"] = max(0.0, o["queue"] - size)
                fill = min(o["remaining"], beyond)
            if fill > 0:
                o["remaining"] -= fill
                pos = self.positions.setdefault(
                    (o["slug"], o["side"]), {"shares": 0.0, "cost": 0.0})
                pos["shares"] += fill
                pos["cost"] += fill * o["level"]
                self._emit("maker_fill", oid=o["oid"], slug=o["slug"],
                           side=o["side"], px=o["level"], shares=fill)
        self.orders = [o for o in self.orders
                       if o["remaining"] > 0 and t_us <= o["expire_us"]]

    # ---- settlement ----------------------------------------------------
    def taker_sell(self, slug, side_label, px, shares, meta=None):
        """Close part or all of a position at `px`, booking realised P&L.

        Without this the jump-scalp's exits were invisible: the exit loop
        called it behind a hasattr guard, the guard failed, and the sale
        was a silent no-op. The position stayed open in the book, so
        settle() later scored shares that had already been sold --
        phantom P&L into day_pnl, which is the number the daily stop
        reads. A scalp that sells at 0.59 for +9c would have been booked
        as a full loss at settlement.

        Cost basis is reduced proportionally, so a partial exit leaves
        the remainder carrying its share of the original cost. Returns
        realised P&L in dollars.
        """
        key = (slug, side_label)
        pos = self.positions.get(key)
        if pos is None or shares <= 0 or px is None:
            return 0.0
        sh = min(float(shares), pos["shares"])
        if sh <= 0:
            return 0.0
        frac = sh / pos["shares"] if pos["shares"] > 0 else 1.0
        basis = pos["cost"] * frac
        fee = 0.07 * px * (1 - px)
        proceeds = sh * (px - fee)
        pos["shares"] -= sh
        pos["cost"] -= basis
        if pos["shares"] <= 1e-9:
            del self.positions[key]
        self._emit("taker_sell", slug=slug, side=side_label, px=px,
                   shares=sh, fee_per_sh=fee, basis=round(basis, 4),
                   proceeds=round(proceeds, 4),
                   pnl=round(proceeds - basis, 4), meta=meta or {})
        return proceeds - basis

    def settle(self, slug, result):
        """result: 0 = Up won, 1 = Down won."""
        pnl = 0.0
        for (s, side), pos in list(self.positions.items()):
            if s != slug or pos["shares"] <= 0:
                continue
            won = (side == "Up" and result == 0) or \
                  (side == "Down" and result == 1)
            payoff = pos["shares"] * (1.0 if won else 0.0)
            pnl += payoff - pos["cost"]
            self._emit("settle", slug=slug, side=side, shares=pos["shares"],
                       cost=pos["cost"], payoff=payoff, won=won)
            del self.positions[(s, side)]
        return pnl
