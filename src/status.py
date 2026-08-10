"""One-shot status: is the bot healthy, and is it finding anything?

Replaces a pile of nested-quote shell one-liners that are fragile to paste.

    python3 src/status.py

Reads the last health line from logs/decisions.jsonl and summarises the
signal funnel, then scores the paper fills against the venue's own settled
outcomes (independent of the bot's internal settlement).
"""
import json
import os
import subprocess
import sys
import time

LOG = "logs/decisions.jsonl"


def last_health():
    if not os.path.exists(LOG):
        return None
    last = None
    with open(LOG) as fh:
        for line in fh:
            if '"health"' in line:
                last = line
    if not last:
        return None
    try:
        return json.loads(last)
    except Exception:  # noqa: BLE001
        return None


def main():
    d = last_health()
    if d is None:
        print("no health line yet — bot may still be starting")
        return
    age = time.time() - d["t_us"] / 1e6
    up = d.get("uptime_s")
    print(f"=== HEALTH  ({age:.0f}s old"
          + (f", bot up {up:.0f}s" if up is not None else "") + ") ===")
    if age > 120:
        print("  !! STALE: no health line in over 2 minutes — bot may be down")
    if up is not None and up < 600:
        print(f"  !! WARM-UP: bot up only {up:.0f}s. The oracle buffer needs "
              f"~10 min to reach back far enough to price a market that is "
              f"REACHING expiry. Counters are not yet meaningful.")
    print(f"  feed      clob_evs={d.get('clob_evs'):,} "
          f"errs={d.get('clob_errs')}  consumer alive={d.get('clob_errs') == 0}")
    rate = d.get("oracle_rate")
    st_ = d.get("stale") or {}
    warn = "  << STALE, z is not trustworthy" if (rate is not None
                                                  and rate < 0.05) else ""
    print(f"  oracle    ticks={d.get('oracle_hist')} "
          f"rate={rate}/s   age={st_.get('oracle_s')}s   "
          f"basis={d.get('basis_n')}{warn}")
    v = d.get("vol_var")
    print(f"  vol       sd={(v ** 0.5):.2e}" if v else "  vol       n/a")
    print(f"  risk      killed={d.get('killed')} day_pnl={d.get('day_pnl')} "
          f"pending_settle={d.get('pending_settle')}")
    ev = d.get("evals") or 0
    f0 = (d.get("funnel") or {}).get("eval", 0)
    rej = ev - f0
    print(f"  activity  evals={ev:,} errs={d.get('eval_errs')} "
          f"signals={d.get('signals')}")
    if ev and rej > 0:
        print(f"            {rej:,} ({rej/ev:.0%}) rejected before the "
              f"strategy: STALE INPUTS")

    f = d.get("funnel") or {}
    if f:
        print("\n=== SIGNAL FUNNEL (why evaluations do or don't fire) ===")
        order = ["eval", "in_window", "priced", "book", "z_pass",
                 "px_pass", "cooldown", "fired"]
        prev = None
        for k in order:
            n = f.get(k, 0)
            drop = ""
            if prev is not None and prev > 0:
                drop = f"   (-{prev - n:,} dropped here)" if n < prev else ""
            print(f"  {k:<10} {n:>8,}{drop}")
            if k not in ("cooldown",):
                prev = n
        diag = []
        if (d.get("oracle_rate") is not None
                and d["oracle_rate"] < 0.05 and f.get("z_pass", 0)):
            diag.append("ORACLE FROZEN: round timestamps are not advancing, "
                        "so z is computed from a stale strike against a live "
                        "spot. z_pass here is an ARTEFACT -- ignore any "
                        "PRICED OUT / firing verdict until rate recovers.")
        elif f.get("in_window", 0) and not f.get("priced", 0):
            diag.append("PRICING BLOCKED: no strike/sigma in the settle "
                        "window — check oracle rate and history depth")
        elif f.get("priced", 0) and not f.get("book", 0):
            diag.append("NO BOOK: two-sided quotes absent when it matters")
        elif f.get("book", 0) and not f.get("z_pass", 0):
            diag.append("SIGNAL NEVER STRONG ENOUGH: |z| < zmin live — "
                        "live sigma may exceed the backtest's; recalibrate")
        elif f.get("z_pass", 0) and not f.get("px_pass", 0):
            diag.append("PRICED OUT: ask above max_price whenever z is "
                        "strong — the edge is not reachable as a taker")
        elif f.get("fired", 0):
            diag.append("firing normally")
        for x in diag:
            print(f"\n  >> {x}")

    print("\n=== PAPER FILLS (scored against the venue's outcomes) ===")
    try:
        subprocess.run([sys.executable, "src/score_paper.py"], check=False)
    except Exception as e:  # noqa: BLE001
        print(f"  scorer failed: {e}")


if __name__ == "__main__":
    main()
