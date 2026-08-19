"""The $0 live-stack validation: every layer below money, certified.

Runs the exact ladder a real order climbs -- env key, L1/L2 auth from
THIS host's IP, account not closed-only, balance visibility, V2 order
build+sign -- and, with --send, submits a deliberately un-crossable
5-share FAK BUY at 0.01 from the (unfunded) wallet. The classification
of the venue's answer is the point:

  balance/allowance rejection  -> FULL PASS: geo, auth, signing, order
                                  format, and Cloudflare all passed; the
                                  only missing layer is money
  geo 403                      -> host/IP problem (the differential
                                  probe says NL passes today)
  auth failure                 -> credential derivation problem
  accepted-but-killed FAK      -> also a pass (order reached matching);
                                  possible only if the wallet has funds

Safety: BUY at 0.01 cannot cross a real book (asks quote >= 0.02), FAK
never rests, and the worst theoretical outcome with a funded wallet is
5 shares x $0.01 = five cents.

    python3 src/probe_live.py            # stops after signing (shadow)
    python3 src/probe_live.py --send     # submits the probe order
"""
import argparse
import json
import sys

import requests

UA = {"User-Agent": "Mozilla/5.0"}
GAMMA = "https://gamma-api.polymarket.com/markets"


def step(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}"
                                                      if detail else ""))
    if not ok:
        sys.exit(1)


def live_btc_token():
    """Up-token id of the currently-open btc 5m market."""
    import time
    t0 = int(time.time()) // 300 * 300
    for t in (t0, t0 - 300):
        r = requests.get(GAMMA, params={"slug": f"btc-updown-5m-{t}"},
                         headers=UA, timeout=15)
        if r.status_code != 200 or not r.json():
            continue
        toks = r.json()[0].get("clobTokenIds")
        if isinstance(toks, str):
            toks = json.loads(toks)
        if toks:
            return str(toks[0]), f"btc-updown-5m-{t}"
    raise RuntimeError("no open btc 5m market found via gamma")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true",
                    help="actually submit the un-crossable probe order")
    a = ap.parse_args()
    sys.path.insert(0, ".")
    from bot.live import LiveExecutor, _key

    k = _key()
    step("env key present", bool(k),
         f"length {len(k)} (value never printed)")

    ex = LiveExecutor(shadow=not a.send)
    try:
        cl = ex.client
    except Exception as e:  # noqa: BLE001
        step("L1/L2 auth (credential derivation)", False, repr(e)[:200])
        return
    step("L1/L2 auth (credential derivation)", True,
         f"wallet {str(cl.wallet())[:10]}... type={cl.wallet_type()}")

    closed = cl.get_closed_only_mode()
    step("account NOT in closed-only mode", not closed, f"closed={closed}")

    try:
        bal = cl.get_balance_allowance(asset_type="COLLATERAL")
        step("balance/allowance visible", True,
             f"balance={getattr(bal, 'balance', bal)}")
    except Exception as e:  # noqa: BLE001
        step("balance/allowance visible", False, repr(e)[:200])

    token, slug = live_btc_token()
    step("live market token fetched", True, slug)

    try:
        signed = cl.create_limit_order(token_id=token, price="0.01",
                                       size="5", side="BUY")
        step("V2 order built and SIGNED", signed is not None)
    except Exception as e:  # noqa: BLE001
        step("V2 order built and SIGNED", False, repr(e)[:250])
        return

    if not a.send:
        print("\nShadow stop: order signed, not sent. Re-run with --send "
              "for the full $0 validation.")
        return

    r = ex.submit_taker(token, "BUY", 5, 0.01, slug=slug)
    d = (r.get("detail") or "").lower()
    if r["status"] == "rejected" and any(
            w in d for w in ("balance", "allowance", "collateral", "fund")):
        print(f"\n  [FULL PASS] venue rejected ONLY for money: {r['detail']}")
        print("  Every layer below funding works from this host. "
              "Fund the wallet and this stack is live.")
    elif r["status"] in ("killed", "filled", "partial"):
        print(f"\n  [PASS] order reached matching: {r}")
    else:
        print(f"\n  [INVESTIGATE] {r['status']}: {r['detail'][:300]}")


if __name__ == "__main__":
    main()
