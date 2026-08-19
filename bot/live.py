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
    def __init__(self, shadow=True):
        self.shadow = shadow
        self._client = None
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        self._log = open(LOG_PATH, "a")

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

    def submit_taker(self, token_id, side, shares, max_price, slug=None):
        """FAK buy of `shares` of `token_id` capped at `max_price`.

        Returns {"status": "shadow"|"filled"|"partial"|"killed"|
                 "rejected"|"error", "filled": float, "detail": str}.
        In shadow mode the order is built and logged but never sent.
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
               "side": side, "shares": float(shares),
               "amount_usd": float(amount), "max_price": float(max_price)}
        if self.shadow:
            self._emit("shadow_order", **req)
            return {"status": "shadow", "filled": 0.0, "detail": "not sent"}
        try:
            r = self.client.place_market_order(
                token_id=str(token_id), side=side, amount=amount,
                max_price=max_price, order_type="FAK")
        except Exception as e:  # noqa: BLE001
            # An un-crossable FAK RAISES InsufficientLiquidityError rather
            # than returning a killed order: that is the venue saying
            # "nothing at your limit", i.e. normal FAK kill semantics.
            if "InsufficientLiquidity" in repr(e):
                self._emit("order_killed_no_liquidity", **req)
                return {"status": "killed", "filled": 0.0,
                        "detail": "no liquidity at limit (FAK kill)"}
            self._emit("order_error", err=repr(e)[:300], **req)
            return {"status": "error", "filled": 0.0,
                    "detail": repr(e)[:300]}
        if not getattr(r, "ok", False):
            self._emit("order_rejected", code=getattr(r, "code", None),
                       message=getattr(r, "message", "")[:300], **req)
            return {"status": "rejected", "filled": 0.0,
                    "detail": f"{getattr(r, 'code', '')}: "
                              f"{getattr(r, 'message', '')}"}
        making = float(getattr(r, "making_amount", 0) or 0)
        taking = float(getattr(r, "taking_amount", 0) or 0)
        # For a BUY: making = collateral given, taking = shares received.
        # The API sometimes reports raw 6-decimal units (the balance
        # endpoint does); normalise heuristically until the first real
        # fills pin it down -- the divergence report recalibrates anyway.
        filled = taking
        if filled > float(shares) * 1000:
            filled = filled / 1e6
        self._emit("order_result", order_id=getattr(r, "order_id", None),
                   status=str(getattr(r, "status", "")),
                   making=making, taking=taking, **req)
        status = "filled" if filled >= float(shares) - 1e-9 else (
            "partial" if filled > 0 else "killed")
        return {"status": status, "filled": filled,
                "detail": str(getattr(r, "status", ""))}
