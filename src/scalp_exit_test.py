"""The scalp exit must rest, book maker fills fee-free, and never
sell the same shares twice.

Four cases, driven through the real scalp_exit_loop against a fake
executor that records every venue call:

  1 rest -> lifted        booked at the target with NO fee, no crossing
  2 rest -> still live    no FAK while the resting order could fill
  3 deadline              cancel is confirmed BEFORE the FAK, once only
  4 cancel unconfirmed    refuses to cross at all

Cases 2-4 are the double-sell paths. Selling the same shares twice
leaves the second sale naked, so these are the assertions that matter;
case 1 exists to prove the maker fill is not booked at the taker fee,
which at an exit price of 0.60 would cost 1.68c/share -- more than the
strategy's whole measured edge.

    python3 src/scalp_exit_test.py
"""
import asyncio
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot.paper import PaperBroker
from bot.state import now_us

import bot.run as R


class FakeExec:
    """Records every venue call so double-sells are visible."""
    def __init__(self, lift_after=None, cancel_ok=True):
        self.calls = []
        self.resting = None
        self.matched = 0.0
        self.lift_after = lift_after       # shares lifted on next poll
        self.cancel_ok = cancel_ok
    def submit_maker_sell(self, token, shares, price, slug=None, outcome=None):
        self.calls.append(("rest", shares, price))
        self.resting = "OID1"
        return {"status": "resting", "filled": 0.0, "avg_px": None,
                "order_id": "OID1", "detail": "live"}
    def order_state(self, oid):
        self.calls.append(("state", oid))
        if self.lift_after is not None:
            self.matched = self.lift_after
        return {"matched": self.matched, "size": 20.0,
                "price": 0.60, "status": "LIVE"}
    def cancel_order(self, oid):
        self.calls.append(("cancel", oid))
        return self.cancel_ok
    def submit_taker_sell(self, token, shares, floor, slug=None, outcome=None):
        self.calls.append(("taker", shares, floor))
        return {"status": "filled", "filled": shares, "avg_px": floor,
                "order_id": "OID2", "detail": "ok"}


def make_bot(ex, deadline_offset_us):
    b = object.__new__(R.Bot)
    b.executor = ex
    b.broker = PaperBroker(log_path=os.path.join(tempfile.mkdtemp(), "fills.jsonl"))
    b._order_pool = None
    b.state = types.SimpleNamespace(markets={})
    b._decisions = []
    b.log_decision = lambda d: b._decisions.append(d)
    b.broker.taker_fills("S", "Up", [(0.51, 20.0)])       # entry
    b._scalps = {("S", "Up"): {
        "slug": "S", "side": "Up", "token": "T", "entry": 0.51,
        "shares": 20.0, "target": 0.60,
        "deadline_us": now_us() + deadline_offset_us, "tries": 0}}
    return b


async def tick(b, n=1):
    task = asyncio.ensure_future(b.scalp_exit_loop())
    await asyncio.sleep(0.6 * n + 0.4)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def main():
    # 1. rests, then the resting sell is lifted -> booked with NO fee
    ex = FakeExec(lift_after=20.0)
    b = make_bot(ex, 60_000_000)
    await tick(b, 4)
    kinds = [d["kind"] for d in b._decisions]
    exits = [d for d in b._decisions if d["kind"] == "scalp_exit"]
    assert ("rest", 20.0, 0.60) in ex.calls, ex.calls
    assert not any(c[0] == "taker" for c in ex.calls), f"crossed out! {ex.calls}"
    assert exits and exits[0]["why"] == "target_maker", kinds
    # 20 sh bought at 0.51 + taker fee, sold 0.60 maker (no fee)
    got = exits[0]["realised"]
    print(f"1. rest+lift  realised={got:+.4f}  calls={[c[0] for c in ex.calls]}")
    # 20 sh: (0.60 - 0.51)*20 = 1.80 gross, minus the 1.75c/sh ENTRY
    # taker fee (0.35) = 1.45. If the exit were booked at the taker rate
    # it would lose a further 0.07*0.6*0.4*20 = 0.34.
    assert abs(got - 1.4501) < 0.002, got
    assert not b._scalps, "position should be closed"

    # 2. never crosses while the resting order is live and unfilled
    ex2 = FakeExec(lift_after=0.0)
    b2 = make_bot(ex2, 60_000_000)
    await tick(b2, 6)
    assert not any(c[0] == "taker" for c in ex2.calls), ex2.calls
    assert b2._scalps, "position should still be open"
    print(f"2. live+unfilled  no taker call  calls={[c[0] for c in ex2.calls]}")

    # 3. deadline -> cancel, reconcile, THEN cross the remainder once
    ex3 = FakeExec(lift_after=0.0)
    b3 = make_bot(ex3, 1_000_000)
    await tick(b3, 5)
    order = [c[0] for c in ex3.calls]
    assert "cancel" in order, order
    assert order.index("cancel") < order.index("taker"), order
    assert sum(1 for c in ex3.calls if c[0] == "taker") == 1, order
    print(f"3. deadline  order={order}")

    # 4. cancel unconfirmed + state unreadable -> refuse to cross
    ex4 = FakeExec(cancel_ok=False)
    ex4.order_state = lambda oid: (ex4.calls.append(("state", oid)), None)[1]
    b4 = make_bot(ex4, 1_000_000)
    await tick(b4, 3)
    assert not any(c[0] == "taker" for c in ex4.calls), ex4.calls
    fails = [d for d in b4._decisions if d["kind"] == "scalp_exit_failed"]
    assert fails and fails[0]["status"] == "cancel_unknown"
    print(f"4. cancel unknown  refused to cross, logged "
          f"{fails[0]['status']}")

    print("\nPASS: rests, books maker fills fee-free, never double-sells")

asyncio.run(main())


# ---- 5/6: a balance rejection is TRANSIENT, not permanent ------------
class FlakyExec(FakeExec):
    """Refuses the resting sell until the CTF balance settles."""
    def __init__(self, fail_n):
        super().__init__(lift_after=0.0)
        self.fail_n, self.attempts = fail_n, 0
    def submit_maker_sell(self, token, shares, price, slug=None, outcome=None):
        self.attempts += 1
        if self.attempts <= self.fail_n:
            self.calls.append(("rest_refused", self.attempts))
            return {"status": "error", "filled": 0.0, "avg_px": None,
                    "order_id": None,
                    "detail": "RequestRejectedError('not enough balance / "
                              "allowance: the balance is not enough -> "
                              "balance: 0, order amount: 18750000')"}
        return super().submit_maker_sell(token, shares, price,
                                         slug=slug, outcome=outcome)


async def extra():
    # settles after 3 refusals -> must still end up resting
    ex = FlakyExec(fail_n=3)
    b = make_bot(ex, 60_000_000)
    await tick(b, 8)
    assert ex.resting == "OID1", f"never rested: {ex.calls}"
    rested = [d for d in b._decisions if d["kind"] == "scalp_rested"]
    assert rested, [d["kind"] for d in b._decisions]
    print(f"5. balance settles late  rested after {rested[0]['tries']} "
          f"tries (refused {ex.attempts - 1}x)")

    # a NON-transient refusal gives up at once and uses the taker
    ex2 = FakeExec()
    ex2.submit_maker_sell = lambda *a, **k: (
        ex2.calls.append(("rest_refused", "hard")),
        {"status": "rejected", "filled": 0.0, "avg_px": None,
         "order_id": None, "detail": "market is closed"})[1]
    b2 = make_bot(ex2, 1_000_000)
    await tick(b2, 4)
    assert sum(1 for c in ex2.calls if c[0] == "rest_refused") == 1, ex2.calls
    assert any(c[0] == "taker" for c in ex2.calls), ex2.calls
    print("6. hard refusal          gave up once, fell back to taker")
    print("\nPASS: transient balance refusals retry; hard ones fall back")

asyncio.run(extra())
