"""Why isn't it trading? One command, read from the live logs.

Every "the bot is taking no trades" question so far has been answered by
one of five counters, spread across two files and a nested health blob.
Chasing them with ad-hoc greps has produced at least two wrong diagnoses
(a `eval_err` pattern that matched `eval_errs`, and a "balance is
draining" read that was really unredeemed winnings). So this reads them
in one pass, in the order a signal actually has to survive:

    eval -> in_window -> priced -> gates -> fired -> order -> fill -> exit

Anything that reads 0 where the stage above it is large is the blocker.

    python3 src/funnel.py                    # default log dir
    python3 src/funnel.py --dir logs/live/btc --orders 8
"""
import argparse
import json
import os
import time


def last_health(path):
    """Newest health line, read from the tail without loading the file."""
    best = None
    with open(path, "rb") as fh:
        try:
            fh.seek(-2_000_000, os.SEEK_END)
            fh.readline()                      # discard the partial line
        except OSError:
            fh.seek(0)
        for raw in fh:
            # Cheap prefilter, then confirm on the PARSED field. Matching
            # the exact byte string '"kind":"health"' would depend on the
            # writer's json separators, and a grep pattern that only looked
            # right has already cost this project one wrong diagnosis.
            if b"health" not in raw:
                continue
            try:
                d = json.loads(raw)
            except Exception:                  # noqa: BLE001
                continue
            if d.get("kind") == "health":
                best = d
    return best


def nonzero(d):
    return {k: v for k, v in (d or {}).items() if v}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="logs/live/btc")
    ap.add_argument("--orders", type=int, default=6)
    a = ap.parse_args()
    dec = os.path.join(a.dir, "decisions.jsonl")
    orders = os.path.join(a.dir, "orders.jsonl")

    h = last_health(dec)
    if h is None:
        print("no health line yet -- the bot has not completed a cycle")
        return
    age = time.time() - h.get("t_us", 0) / 1e6
    print(f"health {age:.0f}s old   evals={h.get('evals')}  "
          f"signals={h.get('signals')}  orders_sent={h.get('orders_sent')}  "
          f"misses={h.get('misses')}")
    print(f"risk: {h.get('risk_state')}  killed={h.get('killed')}  "
          f"day_pnl={h.get('day_pnl')}  halts={h.get('halts')}")
    print(f"feeds: oracle_rate={h.get('oracle_rate')}/s  "
          f"hist={h.get('oracle_hist')}  clob_evs={h.get('clob_evs')}  "
          f"errs={h.get('clob_errs')}  latency={h.get('latency_ms')}ms")

    print("\nSTRATEGY FUNNELS  (first 0 under a big number is the blocker)")
    for name, f in (h.get("funnels") or {}).items():
        row = "  ".join(f"{k}={v}" for k, v in f.items())
        print(f"  {name:<12} {row}")

    pr = nonzero(h.get("price_rej"))
    print(f"\nPRICING REFUSALS  {pr or 'none'}")
    if pr.get("hole"):
        print("   hole: the integral had to hold a price across a gap. "
              "Pre-open that used to mean the un-elapsed strike window.")
    mw = nonzero(h.get("miss_why"))
    print(f"SIGNAL -> ORDER MISSES  {mw or 'none'}")

    if not os.path.exists(orders):
        return
    kinds, recent = {}, []
    for raw in open(orders):
        try:
            d = json.loads(raw)
        except Exception:                      # noqa: BLE001
            continue
        k = d.get("kind", "?")
        kinds[k] = kinds.get(k, 0) + 1
        if k not in ("client_ready",):
            recent.append(d)
    print(f"\nORDER OUTCOMES  {kinds}")
    for d in recent[-a.orders:]:
        t = time.strftime("%H:%M:%S", time.localtime(d.get("t_us", 0) / 1e6))
        err = (d.get("err") or "")[:58]
        print(f"  {t}  {d.get('kind','?'):<26} "
              f"{str(d.get('outcome','')):<5} "
              f"sh={d.get('shares','')} px<={d.get('max_price','')} {err}")

    ex = [json.loads(x) for x in open(dec)
          if '"kind":"scalp_exit' in x]
    if ex:
        done = [e for e in ex if e.get("kind") == "scalp_exit"]
        fail = [e for e in ex if e.get("kind") == "scalp_exit_failed"]
        tot = sum(e.get("realised", 0) or 0 for e in done)
        print(f"\nSCALP EXITS  ok={len(done)}  failed={len(fail)}  "
              f"realised=${tot:+.2f}")
        for e in done[-5:]:
            print(f"  why={e.get('why')} px={e.get('px')} "
                  f"realised={e.get('realised')}")


if __name__ == "__main__":
    main()
