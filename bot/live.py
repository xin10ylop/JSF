"""Live order executor for Polymarket CLOB (CTF Exchange V2), shadow-first.

Isolated from the trading loop ON PURPOSE: bot/run.py stays pure-paper
until the probe script has certified every layer below money on this
host. The integration point later is Bot._execute, where PaperBroker
booking swaps for LiveExecutor.submit_taker.

Modes:
  shadow  build and SIGN a real V2 order, log it, do NOT submit
  live    submit; the CLOB holds crypto up/down takers 250ms and returns
          the final result in the same response, so fills are learned
          synchronously

Secrets: POLYMARKET_PRIVATE_KEY is read from the environment (.env in
the repo root, loaded via python-dotenv). It is never logged, never
printed, and this module never puts it in any string. Use a FRESH
dedicated wallet funded only with the bot's bankroll.
"""
import json
import math
import os
import time

from dotenv import load_dotenv

LOG_PATH = "logs/live_orders.jsonl"


def _key():
    load_dotenv()
    k = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not k:
        raise RuntimeError(
            "POLYMARKET_PRIVATE_KEY not set. Put it in /opt/jsf/.env "
            "(chmod 600), never in the repo or chat.")
    return k


class LiveExecutor:
    def __init__(self, shadow=True, log_path=LOG_PATH):
        self.shadow = shadow
        self._client = None
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        self._log = open(log_path, "a")

    def _emit(self, kind, **kw):
        kw["kind"] = kind
        kw["t_us"] = int(time.time() * 1e6)
        kw["shadow"] = self.shadow
        self._log.write(json.dumps(kw, separators=(",", ":")) + "\n")
        self._log.flush()

    @property
    def client(self):
        """SecureClient, created on first use. create() signs one EIP-712
        auth message with the wallet key and derives the L2 credentials
        (idempotent server-side, safe across restarts).

        POLYMARKET_FUNDER (optional): the Polymarket ACCOUNT address to
        act for -- set it when the key is an exported website-account
        ("magic") key, so the bot trades the same balance the website
        shows, no transfers needed. Left unset, the SDK defaults to the
        signer's own deposit wallet."""
        if self._client is None:
            from polymarket import SecureClient
            k = _key()
            funder = os.environ.get("POLYMARKET_FUNDER") or None
            self._client = SecureClient.create(private_key=k, wallet=funder)
            # wallet / wallet_type are PROPERTIES on this SDK, not methods
            self._emit("client_ready",
                       wallet=str(self._client.wallet),
                       wallet_type=str(self._client.wallet_type),
                       closed_only=bool(self._client.get_closed_only_mode()))
        return self._client

    def submit_taker(self, token_id, side, shares, max_price, slug=None,
                     outcome=None):
        """FAK buy of `shares` of `token_id` capped at `max_price`.

        Returns {"status": "shadow"|"filled"|"partial"|"killed"|
                 "rejected"|"error", "filled": float, "avg_px": float|None,
                 "order_id": str|None, "detail": str}. avg_px is
        making/taking, the matched price per share. The venue charges the
        0.07*p*(1-p) taker fee ON TOP of that, in collateral -- verified
        to the cent against account activity on the first live fills --
        so cost accounting must add the fee; making alone is not the cash
        that leaves.
        In shadow mode the order is built and logged but never sent.
        `outcome` ("Up"/"Down") is log-only, for the divergence report.
        """
        # A FAK-STAMPED LIMIT order at our own seen price, not the SDK's
        # "market order". place_market_order fetches the venue's REST
        # order book before every submission to estimate a price -- a
        # full extra round trip spent INSIDE the race against everyone
        # else hitting the same vanishing ask, and its derived share
        # count sometimes violated the venue's decimal caps ("maker max
        # 2 decimals, taker max 4", one order lost). A limit order signs
        # OUR price and size directly (deterministic venue-clean
        # amounts, zero pre-flight requests), and order_type sits
        # OUTSIDE the EIP-712 signature, so restamping the signed order
        # GTC->FAK is valid: it crosses at prices <= ours or dies, and
        # can never rest on the book.
        # CEIL to the tick, never round. A limit is a CAP: rounding it
        # DOWN puts it under the ask we are trying to lift, and the order
        # is dead before it leaves -- guaranteed, not probabilistic. The
        # venue's tick drops to 0.001 above 0.96, so an ask of 0.974
        # round()ed to 0.97 was pre-killed by arithmetic. Ceiling costs
        # nothing: a CLOB matches a taker at the RESTING maker's price,
        # so a limit above the ask pays the ask, not the limit.
        px = math.ceil(float(max_price) * 100 - 1e-9) / 100.0
        if px > 0.99 + 1e-9:
            # Above the 0.99 cap a 2-decimal limit cannot reach the ask
            # at all. Sending one anyway is a guaranteed kill dressed up
            # as an attempt; refuse instead, so the miss is visible and
            # honest rather than buried in the kill count.
            self._emit("order_skip_above_tick_cap", slug=slug,
                       outcome=outcome, ask=round(float(max_price), 4))
            return {"status": "killed", "filled": 0.0, "avg_px": None,
                    "order_id": None,
                    "detail": f"ask {float(max_price):.4f} above the 0.99 "
                              f"two-decimal cap; no marketable limit"}
        px = round(px, 2)
        c = int(round(px * 100))
        # The venue demands the BUY makerAmount land on whole cents:
        # size*price must have <= 2 decimals, so the allowed share step
        # is 1/gcd(price_cents, 100) -- whole shares at 0.87 or 0.99,
        # 0.1 at 0.90, 0.04 at 0.75. The SDK signs whatever size it is
        # given; 16 fractional-size orders (7.24 sh x 0.87 = $6.2988)
        # died to "maker max accuracy 2 decimals" in the first half hour
        # of the limit path, all in the cheap-dip band. Round DOWN to
        # the step: never oversize, lose at most one step of size.
        step = 1.0 / math.gcd(c, 100)
        sh = round(math.floor(float(shares) / step + 1e-9) * step, 2)
        req = {"slug": slug, "token_id": str(token_id)[:16] + "...",
               "side": side, "outcome": outcome, "shares": sh,
               "amount_usd": round(sh * px, 2), "max_price": px}
        if sh < 5.0:
            # quantization can drop a 5.x request below the venue's
            # 5-share minimum; a $0 non-event, logged as its own kind
            self._emit("order_skip_min_after_quantize", **req)
            return {"status": "killed", "filled": 0.0, "avg_px": None,
                    "order_id": None,
                    "detail": "below venue min after size quantization"}
        if self.shadow:
            self._emit("shadow_order", **req)
            return {"status": "shadow", "filled": 0.0, "avg_px": None,
                    "order_id": None, "detail": "not sent"}
        try:
            from dataclasses import replace
            signed = self.client.create_limit_order(
                token_id=str(token_id), side=side,
                price=f"{px:.2f}", size=f"{sh:.2f}")
            r = self.client.post_order(replace(signed, order_type="FAK"))
        except Exception as e:  # noqa: BLE001
            # An un-crossable FAK surfaces as a RAISE, not a killed-order
            # response, in two shapes: the SDK's InsufficientLiquidityError
            # (pre-flight book check) and the venue's RequestRejectedError
            # "no orders found to match with FAK order" (matching engine
            # found nothing at the limit). Both are normal FAK kill
            # semantics -- zero dollars spent, not an error.
            msg = repr(e)
            low = msg.lower()
            if ("InsufficientLiquidity" in msg
                    or "no orders found to match" in msg
                    or "no match is found" in msg):
                self._emit("order_killed_no_liquidity", err=msg[:200], **req)
                return {"status": "killed", "filled": 0.0, "avg_px": None,
                        "order_id": None,
                        "detail": "no liquidity at limit (FAK kill)"}
            # Documented NON-terminal outcomes (docs/error-codes): the
            # venue may still hold or execute the order, its collateral
            # is reserved, and 'dropping the connection ... won't stop
            # it'. Booking these as kills licenses an immediate re-fire
            # on top of a possibly-live order -- the double-fill path.
            if "order match delayed" in low:
                self._emit("order_pending", err=msg[:200], **req)
                return {"status": "pending", "filled": 0.0, "avg_px": None,
                        "order_id": None,
                        "detail": "match delayed; order still ACTIVE"}
            if ("timed out" in low or "timeout" in low
                    or "transporterror" in low or "connecterror" in low
                    or "readerror" in low or "remoteprotocol" in low):
                self._emit("order_unknown", err=msg[:250], **req)
                return {"status": "unknown", "filled": 0.0, "avg_px": None,
                        "order_id": None,
                        "detail": "outcome UNKNOWN (timeout/transport); "
                                  "venue may still execute"}
            if "429" in msg or "too many" in low or "rate limit" in low \
                    or "post_only" in low or "cancel-only" in low \
                    or "too early" in low:
                self._emit("order_backoff", err=msg[:200], **req)
                return {"status": "backoff", "filled": 0.0, "avg_px": None,
                        "order_id": None, "detail": msg[:200]}
            self._emit("order_error", err=repr(e)[:300], **req)
            return {"status": "error", "filled": 0.0, "avg_px": None,
                    "order_id": None, "detail": repr(e)[:300]}
        if not getattr(r, "ok", False):
            self._emit("order_rejected", code=getattr(r, "code", None),
                       message=getattr(r, "message", "")[:300], **req)
            return {"status": "rejected", "filled": 0.0, "avg_px": None,
                    "order_id": None,
                    "detail": f"{getattr(r, 'code', '')}: "
                              f"{getattr(r, 'message', '')}"}
        st_venue = str(getattr(r, "status", "") or "").lower()
        if st_venue in ("live", "delayed"):
            # Async-pipeline statuses: accepted but not terminally
            # matched here. NOT a kill -- treat as pending and let the
            # caller block re-fires while the venue resolves it.
            self._emit("order_pending",
                       order_id=getattr(r, "order_id", None),
                       status=st_venue,
                       trade_ids=list(getattr(r, "trade_ids", ()) or ()),
                       **req)
            return {"status": "pending", "filled": 0.0, "avg_px": None,
                    "order_id": getattr(r, "order_id", None),
                    "detail": f"venue status {st_venue}"}
        making = float(getattr(r, "making_amount", 0) or 0)
        taking = float(getattr(r, "taking_amount", 0) or 0)
        # For a BUY: making = collateral given, taking = shares received.
        # The API sometimes reports raw 6-decimal units (the balance
        # endpoint does); the SAME scale applies to both sides of one
        # response, so making/taking is a unit-invariant price and only
        # the share count needs the heuristic. Real fills pin it down;
        # the divergence report recalibrates anyway.
        scale = 1e6 if taking > float(sh) * 1000 else 1.0
        making, taking = making / scale, taking / scale
        filled = taking
        avg_px = round(making / taking, 6) if taking > 0 else None
        oid = getattr(r, "order_id", None)
        self._emit("order_result", order_id=oid,
                   status=str(getattr(r, "status", "")),
                   trade_ids=list(getattr(r, "trade_ids", ()) or ()),
                   making=making, taking=taking, avg_px=avg_px, **req)
        status = "filled" if filled >= float(sh) - 1e-9 else (
            "partial" if filled > 0 else "killed")
        if status == "killed":
            # "killed" is the ONE outcome the caller trusts blindly (no
            # refire block, no trade-record verification, retry allowed)
            # -- but this same response object is documented to lie
            # about amounts under the async pipeline (echoed 15 @ 0.99
            # vs actual 393 @ 0.025). A zero parsed amount with a
            # matched status or trade ids attached is NOT a proven kill:
            # hand it to the pending path so the caller blocks re-fires
            # and verifies against venue trade records. A genuine FAK
            # kill has neither.
            tids = list(getattr(r, "trade_ids", ()) or ())
            if tids or st_venue == "matched":
                return {"status": "pending", "filled": 0.0,
                        "avg_px": None, "order_id": oid,
                        "detail": f"zero-amount but status="
                                  f"{st_venue} trade_ids={len(tids)}"}
        return {"status": status, "filled": filled, "avg_px": avg_px,
                "order_id": oid, "detail": str(getattr(r, "status", ""))}
