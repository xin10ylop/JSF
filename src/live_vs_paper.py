"""Live-vs-paper divergence report: the go/no-go gauge for scaling.

The micro-live instances (logs/live/<coin>/) and the frozen paper fleet
(logs/<coin>/) run the SAME code, gates and feeds on the same host; the
only difference is execution. So every gap between the two records is a
measurement of the one thing paper had to assume: fills. This report
puts the two side by side per coin:

  funnel     orders sent / filled / FAK-killed / rejected (live)
  price      share-weighted all-in fill px, live minus paper, on the
             markets BOTH traded -- the direct read of real slippage
             vs the participation model
  per-share  settled P&L per share, each instance on its own record --
             comparable across the 10x size difference
  overlap    markets one instance traded and the other refused --
             signal-level divergence (feed jitter, staleness), which
             should stay small
  recycling  last live balance + redemption count

Run on the droplet:  python3 src/live_vs_paper.py [--hours 24]
"""
import argparse
import json
import os
import time


def read_jsonl(path, since_us):
    if not os.path.exists(path):
        return
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - 64 * 1024 * 1024))
        chunk = fh.read().decode("utf-8", "replace")
    lines = chunk.split("\n")
    for line in (lines[1:] if size > 64 * 1024 * 1024 else lines):
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if d.get("t_us", 0) >= since_us:
            yield d


def fills_of(logdir, since_us):
    """(slug,side) -> [shares, allin_cost]; live real fills carry
    meta.live=True and fee_per_sh=0 with the fee inside px."""
    out, live_only = {}, {}
    for d in read_jsonl(os.path.join(logdir, "paper_fills.jsonl"),
                        since_us):
        if d.get("kind") != "taker_fill":
            continue
        sh = float(d.get("shares", 0))
        allin = float(d.get("px", 0)) + float(d.get("fee_per_sh", 0))
        key = (d.get("slug"), d.get("side"))
        tgt = live_only if (d.get("meta") or {}).get("live") else out
        e = tgt.setdefault(key, [0.0, 0.0])
        e[0] += sh
        e[1] += sh * allin
    return out, live_only


def settles_of(logdir, since_us, only_slugs=None):
    """only_slugs: restrict to these markets -- the live instance's log
    still holds shadow-era SIMULATED settles from before the mode flip,
    and summing them next to real-money settles misled the first live
    readout (+$41 shown for a -$15 reality)."""
    tot, n = 0.0, 0
    sh_settled = 0.0
    for d in read_jsonl(os.path.join(logdir, "decisions.jsonl"), since_us):
        if d.get("kind") == "settled" and d.get("pnl") is not None:
            if only_slugs is not None and d.get("slug") not in only_slugs:
                continue
            tot += float(d["pnl"])
            n += 1
    for d in read_jsonl(os.path.join(logdir, "paper_fills.jsonl"),
                        since_us):
        if d.get("kind") == "settle":
            if only_slugs is not None and d.get("slug") not in only_slugs:
                continue
            sh_settled += float(d.get("shares", 0))
    return tot, n, sh_settled


def orders_of(logdir, since_us):
    c = {"shadow_order": 0, "order_result": 0,
         "order_killed_no_liquidity": 0, "order_rejected": 0,
         "order_error": 0}
    filled_sh = 0.0
    for d in read_jsonl(os.path.join(logdir, "orders.jsonl"), since_us):
        k = d.get("kind")
        if k in c:
            c[k] += 1
        if k == "order_result":
            filled_sh += float(d.get("taking", 0) or 0)
    return c, filled_sh


def maker_sim_of(logdir, since_us):
    """The isolated maker-leg measurement: fills and settles from
    maker_fills.jsonl (simulated against real prints, never touching
    the real-money books)."""
    path = os.path.join(logdir, "maker_fills.jsonl")
    sh = cost = 0.0
    n_orders = n_fills = 0
    pnl = settled_sh = 0.0
    for d in read_jsonl(path, since_us):
        k = d.get("kind")
        if k == "order":
            n_orders += 1
        elif k == "maker_fill":
            n_fills += 1
            s = float(d.get("shares", 0))
            sh += s
            cost += s * float(d.get("px", 0))
        elif k == "settle":
            s = float(d.get("shares", 0))
            settled_sh += s
            pnl += float(d.get("payoff", 0)) - float(d.get("cost", 0))
    return {"orders": n_orders, "fills": n_fills, "sh": sh,
            "avg": (cost / sh) if sh else None,
            "pnl": pnl, "settled_sh": settled_sh}


def last_balance(logdir, since_us):
    bal = None
    for d in read_jsonl(os.path.join(logdir, "decisions.jsonl"), since_us):
        if d.get("kind") == "live_balance":
            bal = d
    return bal


def wavg_px(fills):
    sh = sum(v[0] for v in fills.values())
    return (sum(v[1] for v in fills.values()) / sh, sh) if sh else (None, 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--detail", action="store_true",
                    help="per-market side-by-side rows for shared markets")
    a = ap.parse_args()
    since_us = int((time.time() - a.hours * 3600) * 1e6)
    cfg = json.load(open("bot/config.live.json"))
    coins = cfg.get("coins", ["btc"])
    print(f"live-vs-paper, last {a.hours:.0f}h   "
          f"(config mode: {cfg.get('mode')})")
    agg = {"paper_sh": 0.0, "paper_pnl": 0.0, "live_sh": 0.0,
           "live_pnl": 0.0, "diff_c_w": 0.0, "diff_w": 0.0}
    for coin in coins:
        pdir, ldir = f"logs/{coin}", f"logs/live/{coin}"
        paper, _ = fills_of(pdir, since_us)
        sim, real = fills_of(ldir, since_us)
        live = real if real else sim     # live mode books real; shadow sims
        tag = "real" if real else "sim"
        oc, _osh = orders_of(ldir, since_us)
        p_pnl, p_n, p_sh_set = settles_of(pdir, since_us)
        l_pnl, l_n, l_sh_set = settles_of(
            ldir, since_us,
            only_slugs={k[0] for k in real} if real else None)
        p_px, p_sh = wavg_px(paper)
        l_px, l_sh = wavg_px(live)
        print(f"\n=== {coin} ===")
        print(f"  paper: {p_sh:8.1f} sh / {len({k[0] for k in paper}):3d} "
              f"mkts  avg all-in px "
              f"{p_px:.4f}" if p_px else "  paper: no fills", end="")
        if p_px:
            per = 100 * p_pnl / p_sh_set if p_sh_set else 0.0
            print(f"  settled ${p_pnl:+8.2f} on {p_n} mkts"
                  f" ({per:+.2f}c/sh)")
        else:
            print()
        sent = oc["shadow_order"] + oc["order_result"] + \
            oc["order_killed_no_liquidity"] + oc["order_rejected"] + \
            oc["order_error"]
        print(f"  live orders: sent {sent}  "
              f"(results {oc['order_result']}, "
              f"killed {oc['order_killed_no_liquidity']}, "
              f"rejected {oc['order_rejected']}, "
              f"errors {oc['order_error']}, "
              f"shadow {oc['shadow_order']})")
        if l_px:
            per = 100 * l_pnl / l_sh_set if l_sh_set else 0.0
            print(f"  live({tag}): {l_sh:6.1f} sh / "
                  f"{len({k[0] for k in live}):3d} mkts  avg all-in px "
                  f"{l_px:.4f}  settled ${l_pnl:+8.2f} on {l_n} mkts"
                  f" ({per:+.2f}c/sh)")
        else:
            print(f"  live({tag}): no fills")
        shared = set(paper) & set(live)
        if shared and a.detail:
            print("  --- shared markets, side by side "
                  "(px = all-in per share) ---")
            for k in sorted(shared):
                pp = paper[k][1] / paper[k][0]
                lp = live[k][1] / live[k][0]
                print(f"    {k[0]:<28} {k[1]:<5} "
                      f"paper {paper[k][0]:7.1f}sh @{pp:.4f}   "
                      f"live {live[k][0]:6.1f}sh @{lp:.4f}   "
                      f"diff {100 * (lp - pp):+.2f}c/sh")
        if shared:
            w = sum(min(paper[k][0], live[k][0]) for k in shared)
            diff = sum(min(paper[k][0], live[k][0])
                       * (live[k][1] / live[k][0]
                          - paper[k][1] / paper[k][0]) for k in shared)
            print(f"  shared (slug,side) {len(shared)}: live px - paper px"
                  f" = {100 * diff / w:+.2f}c/sh (weighted, {w:.0f} sh)")
            agg["diff_c_w"] += diff
            agg["diff_w"] += w
        lo = {k[0] for k in live} - {k[0] for k in paper}
        po = {k[0] for k in paper} - {k[0] for k in live}
        print(f"  overlap: live-only mkts {len(lo)}, "
              f"paper-only {len(po)}")
        mk = maker_sim_of(ldir, since_us)
        if mk["orders"] or mk["fills"]:
            per = (100 * mk["pnl"] / mk["settled_sh"]
                   if mk["settled_sh"] else 0.0)
            avg = f"{mk['avg']:.4f}" if mk["avg"] else "-"
            print(f"  maker sim: {mk['orders']} quotes, {mk['fills']} "
                  f"fills / {mk['sh']:.1f} sh @ {avg}, settled "
                  f"${mk['pnl']:+.2f} ({per:+.2f}c/sh)")
        bal = last_balance(ldir, since_us)
        if bal:
            t = time.strftime("%H:%M", time.gmtime(bal["t_us"] / 1e6))
            print(f"  balance ${bal.get('usdc')} "
                  f"(redeemed {bal.get('redeemed')}) at {t} UTC")
        agg["paper_sh"] += p_sh
        agg["paper_pnl"] += p_pnl
        agg["live_sh"] += l_sh
        agg["live_pnl"] += l_pnl
    print("\n=== total ===")
    print(f"  paper ${agg['paper_pnl']:+.2f} on {agg['paper_sh']:.0f} sh"
          f"   live ${agg['live_pnl']:+.2f} on {agg['live_sh']:.0f} sh")
    if agg["diff_w"]:
        print(f"  slippage, live vs paper model: "
              f"{100 * agg['diff_c_w'] / agg['diff_w']:+.2f}c/sh on "
              f"{agg['diff_w']:.0f} comparable sh")
        print("  (negative or ~0 = the paper fill model is honest; "
              "persistently positive = real fills cost more than "
              "modelled -- re-fit participation before scaling)")


if __name__ == "__main__":
    main()
