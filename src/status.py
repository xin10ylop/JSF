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
    # The number to compare against the projection. "Is it tracking?" is a
    # different and more useful question than "is it significant yet?", and
    # only the second one needs a big sample.
    span_h = 0.0
    if "t_us" in sc and sc.t_us.max() > 0:
        span_h = (sc.t_us.max() - sc.t_us.min()) / 1e6 / 3600.0
    if span_h > 0.25:
        mkt_day = sc.slug.nunique() / span_h * 24
        print(f"\n  run rate: ${sc.pnl.sum() / span_h:+,.0f}/hour = "
              f"${sc.pnl.sum() / span_h * 24:+,.0f}/day over {span_h:.1f}h "
              f"of fills   (tape projection: ~$2,100/day at these caps)")
        # Coverage is the other half of the run rate and the one most
        # likely to be leaving money behind: the tape says ~238 btc
        # markets a day carry a qualifying signal, and the old host never
        # got past ~29.
        print(f"  coverage: {sc.slug.nunique()} markets traded = "
              f"{mkt_day:,.0f}/day   (tape: ~238/day for btc alone, "
              f"~700/day across five coins)")
    # 5m and 15m are different contracts (w=30s vs 60s) measured
    # separately: 5m ~$2,236/day, 15m ~$362/day. Keep them apart live too,
    # or a weak family hides inside a strong one.
    fam = sc.slug.str.extract(r"-updown-(\d+m)-", expand=False)
    if fam.nunique() > 1:
        print("\n  by family:")
        for f_ in sorted(fam.dropna().unique()):
            x = sc[fam == f_]
            w_ = x.shares.sum()
            print(f"    {f_:<5} {len(x):>4} fills  {w_:>8,.0f} sh  "
                  f"{x.pnl.sum() / w_ * 100:>+6.2f}c/sh  "
                  f"${x.pnl.sum():>+8.2f}  {x.slug.nunique()} mkts")
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
        print(f"  The fills are real; the SAMPLE is small. {len(g)} markets "
              f"cannot yet separate this from luck (needs |t| ~2, roughly "
              f"30+ markets). It is not evidence against the edge -- the "
              f"run rate above is the thing to watch meanwhile.")
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
    dr, rs = d.get("clob_drops"), d.get("resubs")
    print(f"  feed      clob_evs={d.get('clob_evs'):,} "
          f"errs={d.get('clob_errs')}  consumer alive={d.get('clob_errs') == 0}"
          + (f"  drops={dr} resyncs={rs}" if dr is not None else ""))
    if dr:
        print(f"            !! {dr} message(s) DROPPED on a full queue. Each "
              f"one leaves the level map wrong until the next full snapshot, "
              f"so the bot can price against a book that no longer exists.")
    rate = d.get("oracle_rate")
    st_ = d.get("stale") or {}
    warn = "  << STALE, z is not trustworthy" if (rate is not None
                                                  and rate < 0.05) else ""
    print(f"  oracle    ticks={d.get('oracle_hist')} "
          f"rate={rate}/s   age={st_.get('oracle_s')}s   "
          f"basis={d.get('basis_n')}{warn}")
    # "field absent" and "sigma is None" mean completely different things.
    # An older health line has no sigma_used at all, and treating that as a
    # fault printed "UNUSABLE, z is not priced" against five perfectly
    # healthy bots. Distinguish them.
    has_sigma = "sigma_used" in d
    su, sb = d.get("sigma_used"), d.get("sigma_binance")
    if not has_sigma:
        v = d.get("vol_var")
        print(f"  vol       sd={(v ** 0.5):.2e} (binance estimator; this "
              f"health line predates sigma_used -- restart to see the "
              f"sigma that actually prices z)" if v else "  vol       n/a")
    elif su is not None:
        src = d.get("sigma_src", "?")
        line = f"  vol       sd={su:.2e} (from {src}) <- the one z uses"
        # A 3x gap between the two candidates means one feed is degraded.
        # Harmless while the good one is preferred, and a live fault the
        # moment it is not.
        if sb and (sb / su > 3 or su / sb > 3):
            # Always report the ratio as "N x apart" with N >= 1. Dividing
            # the small by the large printed "0.0x apart" for a 58x gap,
            # which reads like agreement.
            r = max(sb / su, su / sb)
            line += (f"   [binance reads {sb:.2e}, {r:.0f}x apart "
                     f"-- that estimator is broken, see OnlineVol.update]")
        print(line)
    else:
        print("  vol       !! sigma_rel() returned None -- BOTH estimators "
              "are outside the plausibility band, so nothing can be priced")
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
