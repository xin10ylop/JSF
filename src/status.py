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

import glob


def health_logs():
    """Every per-coin decisions log, plus the legacy flat one."""
    paths = sorted(glob.glob("logs/*/decisions.jsonl"))
    if os.path.exists("logs/decisions.jsonl"):
        paths.append("logs/decisions.jsonl")
    return paths


def last_health(path):
    if not os.path.exists(path):
        return None
    last = None
    with open(path) as fh:
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
    paths = health_logs()
    if not paths:
        print("no decisions log yet — no bot has started")
        return
    for i, p in enumerate(paths):
        d = last_health(p)
        # logs/<coin>/decisions.jsonl -> <coin>. The flat legacy path has
        # no coin directory and predates the `coin` health field.
        parts = p.split(os.sep)
        coin = ((d or {}).get("coin")
                or (parts[-2] if len(parts) > 2 else "btc (legacy log)"))
        if i:
            print()
        print(f"########## {coin.upper()} ".ljust(60, "#"))
        if d is None:
            print("no health line yet — bot may still be starting")
            continue
        show(d)
    print("\n=== PAPER FILLS (scored against the venue's outcomes) ===")
    try:
        subprocess.run([sys.executable, "src/score_paper.py"], check=False)
    except Exception as e:  # noqa: BLE001
        print(f"  scorer failed: {e}")


def show(d):
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
    lat = d.get("latency_ms")
    print(f"  orders    pending={d.get('pending_orders')} "
          f"MISSES={d.get('misses')}   <- asks gone by the time we arrive")
    sent = d.get("orders_sent")
    print(f"            sent={sent} partials={d.get('partials')} "
          f"venue_rejects={d.get('venue_rejects')}"
          + (f"   delay={lat}ms (venue hold + our RTT)" if lat else ""))
    # The denominator must be orders actually SENT. Most strategy fires
    # never become orders -- once a market hits its dollar cap every later
    # signal is sized to zero -- so comparing misses to `fired` reported a
    # 0% miss rate off two orders and looked like a broken fill model.
    ms = d.get("misses")
    if sent and ms is not None and sent >= 50 and ms / sent < 0.15:
        print(f"            !! only {ms/sent:.0%} of SENT orders miss. "
              f"Recorded books say ~35% of asks in the 0.92-0.99 band are "
              f"gone within 400ms — a low miss rate means the fill model "
              f"is still too kind, not that we are fast.")
    if ev and rej > 0:
        rj = d.get("rejects") or {}
        det = " ".join(f"{k}={v:,}" for k, v in rj.items() if v)
        print(f"            {rej:,} ({rej/ev:.0%}) rejected pre-strategy"
              + (f"  [{det}]" if det else ""))
        if rj.get("book", 0) > 0.5 * rej:
            print("            (mostly BOOK staleness on markets not yet in "
                  "their window - expected, not a fault)")

    f = d.get("funnel") or {}
    if f:
        print("\n=== SIGNAL FUNNEL (why evaluations do or don't fire) ===")
        # Every counter here is a PASS count except `cooldown`, which the
        # strategy increments on its two rejection paths. Rendering it in
        # the pass chain printed "cooldown 0 (-1,037 dropped here)" -- which
        # reads as the re-entry gate killing every signal when in fact it
        # killed none. Show it separately, labelled for what it is.
        order = ["eval", "in_window", "priced", "book", "z_pass",
                 "px_pass", "fired"]
        prev = None
        for k in order:
            n = f.get(k, 0)
            drop = ""
            if prev is not None and prev > n:
                drop = f"   (-{prev - n:,} dropped here)"
            print(f"  {k:<10} {n:>8,}{drop}")
            prev = n
        cd = f.get("cooldown", 0)
        print(f"  {'(re-entry rejects: ' + format(cd, ',') + ')':<10}"
              f"   <- same book snapshot; not part of the chain above")
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
            diag.append(
                "PRICED OUT: the favoured side's ask was above max_price "
                "on every strong-z evaluation so far. Expected some of the "
                "time -- when |z| is huge the market is already decided and "
                "quotes 0.995+. Only a problem if it persists for hours; "
                "the tape says the 0.98-1.00 band alone carries 1.69M "
                "qualifying shares over four days.")
        elif f.get("fired", 0):
            diag.append("firing normally")
        for x in diag:
            print(f"\n  >> {x}")


if __name__ == "__main__":
    main()
