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
        # The SDK denominates BUY market orders in DOLLARS: amount is the
        # spend, max_price caps the per-share price. shares*max_price
        # therefore buys AT LEAST `shares` (more if the book is better,
        # which is strictly good EV) and can never spend beyond the
        # dollar figure -- the same quantity risk.max_market_dollars caps.
        from decimal import Decimal, ROUND_DOWN
        amount = (Decimal(str(shares)) * Decimal(str(max_price))
                  ).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        req = {"slug": slug, "token_id": str(token_id)[:16] + "...",
               "side": side, "outcome": outcome, "shares": float(shares),
               "amount_usd": float(amount), "max_price": float(max_price)}
        if self.shadow:
            self._emit("shadow_order", **req)
            return {"status": "shadow", "filled": 0.0, "avg_px": None,
                    "order_id": None, "detail": "not sent"}
        try:
            r = self.client.place_market_order(
                token_id=str(token_id), side=side, amount=amount,
                max_price=max_price, order_type="FAK")
        except Exception as e:  # noqa: BLE001
            # An un-crossable FAK surfaces as a RAISE, not a killed-order
            # response, in two shapes: the SDK's InsufficientLiquidityError
            # (pre-flight book check) and the venue's RequestRejectedError
            # "no orders found to match with FAK order" (matching engine
            # found nothing at the limit). Both are normal FAK kill
            # semantics -- zero dollars spent, not an error.
            msg = repr(e)
            if ("InsufficientLiquidity" in msg
                    or "no orders found to match" in msg
                    or "no match is found" in msg):
                self._emit("order_killed_no_liquidity", err=msg[:200], **req)
                return {"status": "killed", "filled": 0.0, "avg_px": None,
                        "order_id": None,
                        "detail": "no liquidity at limit (FAK kill)"}
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
        making = float(getattr(r, "making_amount", 0) or 0)
        taking = float(getattr(r, "taking_amount", 0) or 0)
        # For a BUY: making = collateral given, taking = shares received.
        # The API sometimes reports raw 6-decimal units (the balance
        # endpoint does); the SAME scale applies to both sides of one
        # response, so making/taking is a unit-invariant price and only
        # the share count needs the heuristic. Real fills pin it down;
        # the divergence report recalibrates anyway.
        scale = 1e6 if taking > float(shares) * 1000 else 1.0
        making, taking = making / scale, taking / scale
        filled = taking
        avg_px = round(making / taking, 6) if taking > 0 else None
        oid = getattr(r, "order_id", None)
        self._emit("order_result", order_id=oid,
                   status=str(getattr(r, "status", "")),
                   making=making, taking=taking, avg_px=avg_px, **req)
        status = "filled" if filled >= float(shares) - 1e-9 else (
            "partial" if filled > 0 else "killed")
        return {"status": status, "filled": filled, "avg_px": avg_px,
                "order_id": oid, "detail": str(getattr(r, "status", ""))}
