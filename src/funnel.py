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


def tail_records(path, kinds, nbytes=4_000_000):
    """Parsed records of the given kinds from the tail of a jsonl log.

    Kinds are checked on the PARSED field. Matching a raw byte string like
    '"kind":"health"' would depend on the writer's json separators, and a
    pattern that only looked right has already cost this project one wrong
    diagnosis (`eval_err` silently matching `eval_errs`).
    """
    out = []
    with open(path, "rb") as fh:
        try:
            fh.seek(-nbytes, os.SEEK_END)
            fh.readline()                      # discard the partial line
        except OSError:
            fh.seek(0)
        for raw in fh:
            try:
                d = json.loads(raw)
            except Exception:                  # noqa: BLE001
                continue
            if d.get("kind") in kinds:
                out.append(d)
    return out


def med(v):
    v = sorted(x for x in v if isinstance(x, (int, float)))
    return v[len(v) // 2] if v else None


def nonzero(d):
    return {k: v for k, v in (d or {}).items() if v}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="logs/live/btc")
    ap.add_argument("--orders", type=int, default=6)
    a = ap.parse_args()
    dec = os.path.join(a.dir, "decisions.jsonl")
    orders = os.path.join(a.dir, "orders.jsonl")

    recs = tail_records(dec, {"health", "live_miss", "taker_miss",
                              "scalp_exit", "scalp_exit_failed",
                              "live_balance", "redeem_err",
                              "redeem_list_err", "cash_kill",
                              "scalp_rested", "scalp_rest_failed"})
    heals = [r for r in recs if r.get("kind") == "health"]
    h = heals[-1] if heals else None
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
    # A signal sized to ZERO never reaches the dispatcher, so it lands in
    # `rejects`, not in miss_why -- and rejects["size"] is precisely the
    # concurrency/cap starvation case (max_concurrent markets already hold
    # positions, so every new market sizes to nothing). Leaving it out
    # made a fully starved bot look identical to an idle one.
    print(f"PRE-DISPATCH REJECTS   {nonzero(h.get('rejects')) or 'none'}")
    if (h.get("rejects") or {}).get("size"):
        print("   size: sized to 0 -- per-market cap full, or "
              "max_concurrent markets already hold positions")
    print(f"OPEN STATE  pending_settle={h.get('pending_settle')}  "
          f"pending_orders={h.get('pending_orders')}  "
          f"inflight={h.get('inflight')}  "
          f"venue_rejects={h.get('venue_rejects')}  "
          f"partials={h.get('partials')}")

    # Winnings arrive as conditional tokens and are only cash once
    # redeemed, so the wallet's USDC can sit flat while the portfolio
    # grows -- that gap already produced one wrong "the balance is
    # draining" read. `redeemed: 0` with no error is ambiguous between
    # "nothing was redeemable" and "the listing call returned nothing it
    # should have", so show the errors alongside it rather than either
    # counter alone.
    bals = [r for r in recs if r.get("kind") == "live_balance"]
    if bals:
        first, last = bals[0], bals[-1]
        tot_red = sum(r.get("redeemed", 0) or 0 for r in bals)
        print(f"\nWALLET  usdc={last.get('usdc')}  "
              f"(from {first.get('usdc')} over {len(bals)} ops passes)  "
              f"redeemed_total={tot_red}")
        rerr = [r for r in recs
                if r.get("kind") in ("redeem_err", "redeem_list_err")]
        if rerr:
            print(f"  REDEEM ERRORS n={len(rerr)}: "
                  f"{(rerr[-1].get('err') or '')[:110]}")
        elif tot_red == 0:
            print("  no redeem errors and nothing redeemed -- the venue "
                  "reported no redeemable positions")
    for r in recs:
        if r.get("kind") == "cash_kill":
            print(f"  CASH KILL: bal={r.get('bal')} floor={r.get('floor')}")

    # `too_small` is raised by two different code paths -- the live
    # dispatcher (size under the venue's 5-share orderMinSize) and the
    # shadow/paper sizer (want under min_order_size after the book, tape
    # and participation caps). The counter cannot tell them apart, and
    # they have completely different fixes, so break it out by the `why`
    # each path logs and show which cap actually bound.
    misses = [r for r in recs if r.get("kind") in ("live_miss", "taker_miss")]
    if misses:
        by = {}
        for r in misses:
            by.setdefault((r["kind"], r.get("why", "?")), []).append(r)
        print("\nMISS DETAIL  (tail of the log, newest window)")
        for (kind, why), v in sorted(by.items(), key=lambda kv: -len(kv[1])):
            line = f"  {kind:<11} {why:<18} n={len(v):<5}"
            if why == "below_min_size":
                line += (f" want={med([x.get('want') for x in v])}"
                         f"  cap_book={med([x.get('cap_book') for x in v])}"
                         f"  cap_tape={med([x.get('cap_tape') for x in v])}"
                         f"  cap_life={med([x.get('cap_life') for x in v])}")
            elif why == "below_venue_min":
                line += f" size={med([x.get('size') for x in v])}"
            elif why == "book_view_stale":
                line += f" age_s={med([x.get('age_s') for x in v])}"
            print(line)
        print("   the smallest cap is the binding one")

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
    # Surface WHY a resting sell was refused. The maker exit is worth
    # +0.40c/share over crossing out, and it fails silently into the
    # taker fallback, so the reason has to be visible here or the
    # degradation never gets noticed.
    merr = [d for d in recent if d.get("kind") in
            ("maker_sell_error", "maker_sell_rejected", "maker_sell_skip_min",
             "maker_sell_skip_tick", "cancel_err", "order_state_err")]
    if merr:
        print(f"  RESTING-SELL FAILURES n={len(merr)}")
        seen = set()
        for d in merr[-4:]:
            msg = (d.get("err") or d.get("message") or d.get("detail") or "")
            key = (d.get("kind"), msg[:60])
            if key in seen:
                continue
            seen.add(key)
            print(f"    {d.get('kind')}: {msg[:150]}")
    for d in recent[-a.orders:]:
        t = time.strftime("%H:%M:%S", time.localtime(d.get("t_us", 0) / 1e6))
        err = (d.get("err") or "")[:58]
        print(f"  {t}  {d.get('kind','?'):<26} "
              f"{str(d.get('outcome','')):<5} "
              f"sh={d.get('shares','')} px<={d.get('max_price','')} {err}")

    ex = [r for r in recs
          if r.get("kind") in ("scalp_exit", "scalp_exit_failed")]
    if ex:
        done = [e for e in ex if e.get("kind") == "scalp_exit"]
        fail = [e for e in ex if e.get("kind") == "scalp_exit_failed"]
        tot = sum(e.get("realised", 0) or 0 for e in done)
        print(f"\nSCALP EXITS  ok={len(done)}  failed={len(fail)}  "
              f"realised=${tot:+.2f}")
        # Per-scalp chain. A scalp is capped at +9c on the upside and
        # UNCAPPED on the downside -- it exits at whatever the book pays
        # at t+30 -- so one bad exit erases several good ones. Showing
        # entry/target/exit together is the only way to see whether a
        # loss was the strategy working as designed or the exit failing.
        rested = [r for r in recs if r.get("kind") == "scalp_rested"]
        restf = [r for r in recs if r.get("kind") == "scalp_rest_failed"]
        print(f"  resting sells posted={len(rested)} failed={len(restf)}"
              + (f"  (settled after {med([x.get('tries') for x in rested])}"
                 f" ticks)" if rested else ""))
        for r in restf[-2:]:
            print(f"    rest_failed tries={r.get('tries')} "
                  f"gave_up={r.get('gave_up')} {(r.get('detail') or '')[:90]}")
        for e in done[-5:]:
            # the log key is `exit`, not `px`
            en, ex = e.get("entry"), e.get("exit")
            move = (f"{100*(ex-en):+.1f}c" if isinstance(en, (int, float))
                    and isinstance(ex, (int, float)) else "?")
            print(f"  why={e.get('why'):<18} entry={en} exit={ex} "
                  f"({move})  sh={e.get('shares')} "
                  f"realised={e.get('realised')}")
        if not any(e.get("why", "").startswith("target_maker") for e in done):
            print("  NOTE: no maker exits -- the resting sell is not "
                  "working; these are taker fallbacks")


if __name__ == "__main__":
    main()
