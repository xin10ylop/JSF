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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def pnl_by_bot():
    """The number you actually came here for, per bot, at the top.

    Previously this ran score_paper.py as a subprocess. Python buffers its
    own prints while a child writes straight to the terminal, so the whole
    fills section appeared ABOVE the health blocks -- it looked like noise
    and read as if there were no P&L at all.
    """
    try:
        import pandas as pd
        import score_paper as sp
    except Exception as e:  # noqa: BLE001
        print(f"  cannot load the scorer: {e}")
        return None
    a = sp.parser().parse_args([])
    fills = sp.gather(a)
    if not fills:
        print("  no latency-realistic fills on a fresh oracle yet")
        return None
    rows = sp.score_rows(fills)
    d = pd.DataFrame(rows)
    sc = d[d.status == "scored"]
    un = d[d.status == "unresolved"]
    if not len(sc):
        print(f"  {len(un)} fill(s) taken, none settled yet — the venue "
              f"resolves a market ~1 min after it closes")
        return d
    print(f"{'bot':<7}{'fills':>6}{'shares':>9}{'avg px':>8}{'hit':>7}"
          f"{'c/share':>9}{'P&L $':>9}{'mkts':>6}{'open':>6}")
    for coin in sorted(sc.coin.unique()):
        x = sc[sc.coin == coin]
        w = x.shares.sum()
        print(f"{coin:<7}{len(x):>6,}{w:>9,.0f}"
              f"{(x.px * x.shares).sum() / w:>8.4f}"
              f"{(x.won * x.shares).sum() / w:>7.3f}"
              f"{x.pnl.sum() / w * 100:>+9.2f}{x.pnl.sum():>+9.2f}"
              f"{x.slug.nunique():>6}{(un.coin == coin).sum():>6}")
    w = sc.shares.sum()
    print(f"{'TOTAL':<7}{len(sc):>6,}{w:>9,.0f}"
          f"{(sc.px * sc.shares).sum() / w:>8.4f}"
          f"{(sc.won * sc.shares).sum() / w:>7.3f}"
          f"{sc.pnl.sum() / w * 100:>+9.2f}{sc.pnl.sum():>+9.2f}"
          f"{sc.slug.nunique():>6}{len(un):>6}")
    # Fills inside one market settle on one outcome, so the honest
    # denominator is markets, not fills.
    g = sc.groupby("slug").agg(pnl=("pnl", "sum"), sh=("shares", "sum"))
    tcl = None
    if len(g) > 2:
        mu = g.pnl.sum() / g.sh.sum()
        se = ((g.pnl - mu * g.sh) ** 2).sum() ** 0.5 / g.sh.sum()
        if se > 0:
            tcl = mu / se
            print(f"\n  t = {tcl:+.2f} clustered by market "
                  f"(n={len(g)} markets, NOT {len(sc)} fills)")
    if tcl is not None and abs(tcl) < 2.0:
        print(f"  NOT SIGNIFICANT YET. {len(g)} markets is a handful; the "
              f"dollar figure above is noise until |t| clears ~2.")
    # A big headline built out of a few cheap longshots is not this
    # strategy. Sub-0.50 fills are the bucket whose PRE-change control was
    # also positive (+2.24c/share), i.e. a different effect that predates
    # the contract change -- so it must never be allowed to masquerade as
    # the settlement edge in the summary line.
    ls = sc[sc.px < 0.5]
    if len(ls) and sc.pnl.sum() > 0:
        frac = ls.pnl.sum() / sc.pnl.sum()
        if frac > 0.4:
            print(f"  !! {frac:.0%} of that P&L is {len(ls)} fill(s) below "
                  f"0.50 ({ls.slug.nunique()} market(s)). That is the "
                  f"LONGSHOT effect, which was positive pre-change too -- "
                  f"not the settlement edge. Excluding them: "
                  f"${sc[sc.px >= 0.5].pnl.sum():+,.2f} on "
                  f"{sc[sc.px >= 0.5].shares.sum():,.0f} shares "
                  f"({sc[sc.px >= 0.5].pnl.sum() / max(sc[sc.px >= 0.5].shares.sum(), 1) * 100:+.2f}c/share).")
    return d


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
    print("########## P&L BY BOT ".ljust(60, "#"))
    pnl_by_bot()
    print()
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
    print("\n=== PAPER FILLS, in detail "
          "(scored against the venue's outcomes) ===")
    sys.stdout.flush()      # the child writes to the fd directly; without
                            # this its output lands above everything above
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
    mw = d.get("miss_why") or {}
    if any(mw.values()):
        tot = sum(mw.values()) or 1
        print("            why: "
              + "  ".join(
                  f"{k}={v} ({v/tot:.0%})" for k, v in mw.items() if v))
        if mw.get("too_small", 0) > 0.5 * tot:
            print("            (mostly OUR OWN caps, not the venue: the "
                  "fill model allows a quarter of what printed at our "
                  "limit in the last 5s, and below 5 shares the venue "
                  "would reject the order anyway. Thin coins hit this.)")
        elif mw.get("ask_gone", 0) > 0.5 * tot:
            print("            (mostly the ask MOVING inside our 276ms "
                  "delay — the real race, and the thing co-location buys)")
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
