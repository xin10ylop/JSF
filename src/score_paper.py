"""Score paper fills INDEPENDENTLY of the bot.

The bot settles positions from its own oracle buffer. If that buffer lacks
coverage (a restart, a feed gap) a fill can go unscored and silently vanish
from the record — observed live: a 100-share fill at 0.37 with no settle
line. Any performance read built on the bot's own settlement inherits that
bias.

This reads logs/paper_fills.jsonl and scores every taker fill against the
venue's resolved outcome from the free Gamma API. It does not trust, or
need, the bot's settle lines.

Run: python3 src/score_paper.py [--log logs/paper_fills.jsonl]
"""
import argparse
import json
import os
from collections import defaultdict

import pandas as pd
import requests

GAMMA = "https://gamma-api.polymarket.com/markets"
FEE = 0.07
UA = {"User-Agent": "Mozilla/5.0"}


def outcomes_for(slugs, session):
    """slug -> True if Up won, via the venue's settled outcomePrices."""
    out = {}
    slugs = sorted(set(slugs))
    for i in range(0, len(slugs), 100):
        chunk = slugs[i:i + 100]
        params = [("slug", s) for s in chunk]
        params += [("closed", "true"), ("limit", "500")]
        try:
            r = session.get(GAMMA, params=params, timeout=45)
            if r.status_code != 200:
                continue
            for m in r.json():
                op = m.get("outcomePrices")
                oc = m.get("outcomes")
                if isinstance(op, str):
                    op = json.loads(op)
                if isinstance(oc, str):
                    oc = json.loads(oc)
                if not op:
                    continue
                iu = oc.index("Up") if oc and "Up" in oc else 0
                out[m["slug"]] = float(op[iu]) > 0.5
        except Exception:  # noqa: BLE001
            continue
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="logs/paper_fills.jsonl")
    # The fill log accumulates across builds AND strategies. Scoring all of
    # it mixes, e.g., old vacuum-ladder maker fills at ~0.21 with the
    # current endgame taker -- a meaningless blend. Default to the strategy
    # in production; widen deliberately.
    ap.add_argument("--reason-prefix", default="rollavg",
                    help="only fills whose reason starts with this ('' = all)")
    ap.add_argument("--since", default=None,
                    help="only fills at/after this UTC time, YYYY-MM-DDTHH:MM")
    ap.add_argument("--all-oracle", action="store_true",
                    help="include fills taken on a stale/unstamped oracle "
                         "(default: only fills stamped fresh)")
    a = ap.parse_args()
    since_us = None
    if a.since:
        import datetime as _dt
        since_us = int(_dt.datetime.strptime(a.since, "%Y-%m-%dT%H:%M")
                       .replace(tzinfo=_dt.timezone.utc).timestamp() * 1e6)
    if not os.path.exists(a.log):
        print(f"no fill log at {a.log}")
        return
    fills = []
    for line in open(a.log):
        try:
            d = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if d.get("kind") not in ("taker_fill", "maker_fill"):
            continue
        if a.reason_prefix:
            r = (d.get("meta") or {}).get("reason", "")
            if not r.startswith(a.reason_prefix):
                continue
        if since_us and d.get("t_us", 0) < since_us:
            continue
        if not a.all_oracle:
            oa = (d.get("meta") or {}).get("oracle_age_s")
            # Unstamped fills predate the freshness stamp and were taken
            # while the oracle could silently freeze -- unusable evidence.
            if oa is None or oa > 20:
                continue
        fills.append(d)
    if not fills:
        print("no fills yet on a verified-fresh oracle "
              "(use --all-oracle to see the contaminated history)"
              if not a.all_oracle else
              f"no fills matching reason_prefix={a.reason_prefix!r}"
              + (f" since {a.since}" if a.since else ""))
        return
    print(f"(fresh-oracle fills only; --all-oracle to include the rest)"
          if not a.all_oracle else "(ALL fills, including stale-oracle)")
    print(f"(filtered to reason_prefix={a.reason_prefix!r}"
          + (f", since {a.since}" if a.since else "") + ")")
    s = requests.Session()
    s.headers.update(UA)
    res = outcomes_for([f["slug"] for f in fills], s)

    rows = []
    for f in fills:
        up_won = res.get(f["slug"])
        if up_won is None:
            rows.append({**f, "status": "unresolved"})
            continue
        won = up_won if f["side"] == "Up" else (not up_won)
        px = float(f["px"])
        sh = float(f["shares"])
        fee = float(f.get("fee_per_sh", FEE * px * (1 - px)))
        rows.append({"slug": f["slug"], "side": f["side"], "px": px,
                     "shares": sh, "fee": fee, "won": bool(won),
                     "pnl": sh * (float(won) - px - fee),
                     "reason": (f.get("meta") or {}).get("reason", "")[:60],
                     "status": "scored"})
    d = pd.DataFrame(rows)
    sc = d[d.status == "scored"]
    un = d[d.status == "unresolved"]
    print(f"fills={len(d)}  scored={len(sc)}  unresolved={len(un)} "
          f"(markets still open)")
    if not len(sc):
        return
    print(f"\nshares={sc.shares.sum():,.0f}  avg px {(sc.px*sc.shares).sum()/sc.shares.sum():.4f}"
          f"  hit {(sc.won*sc.shares).sum()/sc.shares.sum():.3f}")
    print(f"P&L ${sc.pnl.sum():+,.2f}  "
          f"= {sc.pnl.sum()/sc.shares.sum()*100:+.2f}c/share")
    print(f"per-fill mean ${sc.pnl.mean():+.2f}  sd ${sc.pnl.std():,.2f}")
    if len(sc) > 2:
        se = sc.pnl.std() / (len(sc) ** 0.5)
        print(f"  t = {sc.pnl.mean()/se:+.2f} on {len(sc)} fills")
    print("\nby fill price:")
    b = pd.cut(sc.px, [0, .3, .5, .7, .85, .92, .95, .98, 1.0])
    print(sc.groupby(b, observed=True).apply(lambda x: pd.Series({
        "fills": len(x), "shares": x.shares.sum(),
        "hit": (x.won * x.shares).sum() / x.shares.sum(),
        "c_per_sh": x.pnl.sum() / x.shares.sum() * 100,
        "pnl": x.pnl.sum()})).to_string(float_format=lambda v: f"{v:,.2f}"))
    per_mkt = sc.groupby("slug").shares.sum()
    print(f"\nmarkets traded {len(per_mkt)}   shares/market: "
          f"mean {per_mkt.mean():.0f} median {per_mkt.median():.0f} "
          f"max {per_mkt.max():.0f}")
    print("\nlast 8 fills:")
    print(sc.tail(8)[["slug", "side", "px", "shares", "won", "pnl"]]
          .to_string(index=False, float_format=lambda v: f"{v:,.3f}"))


if __name__ == "__main__":
    main()
