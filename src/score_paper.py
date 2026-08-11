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
import glob
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


def parser():
    ap = argparse.ArgumentParser()
    # One process per coin means one fill log per coin. Default to all of
    # them: the whole point of the multi-coin build is the aggregate, and a
    # scorer that silently reads btc only would hide four fifths of it.
    ap.add_argument("--log", default=None,
                    help="fill log(s); default: logs/*/paper_fills.jsonl "
                         "plus the legacy logs/paper_fills.jsonl")
    ap.add_argument("--coin", default=None,
                    help="restrict to one coin's log")
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
    # Fills before the latency model executed instantly at the price the bot
    # SAW. That is the single biggest way paper flattered reality (63% of an
    # overnight +$1,331 came from sub-0.85 dips that vanish in <150ms), and
    # 488 such fills would otherwise dominate the average for hours.
    ap.add_argument("--all-latency", action="store_true",
                    help="include zero-latency fills from before the latency "
                         "model (default: only latency-realistic fills)")
    return ap


def gather(a):
    """Read every matching fill out of the logs. Shared with src/status.py
    so the per-bot P&L table and the detailed report cost one pass, not
    two -- and can never disagree with each other."""
    since_us = None
    if a.since:
        import datetime as _dt
        since_us = int(_dt.datetime.strptime(a.since, "%Y-%m-%dT%H:%M")
                       .replace(tzinfo=_dt.timezone.utc).timestamp() * 1e6)
    if a.log:
        logs = [a.log]
    elif a.coin:
        logs = [f"logs/{a.coin}/paper_fills.jsonl"]
    else:
        logs = sorted(glob.glob("logs/*/paper_fills.jsonl"))
        if os.path.exists("logs/paper_fills.jsonl"):
            logs.append("logs/paper_fills.jsonl")
    logs = [p for p in logs if os.path.exists(p)]
    if not logs:
        print("no fill log found (looked for logs/*/paper_fills.jsonl)")
        return []
    fills = []
    for path in logs:
        # logs/<coin>/paper_fills.jsonl -> <coin>; legacy flat log -> btc
        parts = path.split(os.sep)
        coin = parts[-2] if len(parts) > 2 and parts[-2] != "logs" else "btc"
        for line in open(path):
            try:
                d = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if d.get("kind") not in ("taker_fill", "maker_fill"):
                continue
            d["coin"] = coin
            _keep(d, a, since_us, fills)
    return fills


def main():
    a = parser().parse_args()
    _report(gather(a), a)


def _keep(d, a, since_us, fills):
    meta = d.get("meta") or {}
    if a.reason_prefix and not meta.get("reason", "").startswith(
            a.reason_prefix):
        return
    if since_us and d.get("t_us", 0) < since_us:
        return
    if not a.all_latency and meta.get("seen_px") is None:
        return
    if not a.all_oracle:
        oa = meta.get("oracle_age_s")
        # Unstamped fills predate the freshness stamp and were taken while
        # the oracle could silently freeze -- unusable evidence.
        if oa is None or oa > 20:
            return
    fills.append(d)


def score_rows(fills):
    """Resolve every fill against the venue's settled outcome.

    Split out of _report so src/status.py can render the per-bot P&L table
    from the same pass -- one Gamma fetch, and the summary can never
    disagree with the detail below it.
    """
    s = requests.Session()
    s.headers.update(UA)
    res = outcomes_for([f["slug"] for f in fills], s)
    rows = []
    for f in fills:
        up_won = res.get(f["slug"])
        coin = f.get("coin", "btc")
        if up_won is None:
            rows.append({"coin": coin, "slug": f["slug"], "side": f["side"],
                         "px": float(f["px"]), "shares": float(f["shares"]),
                         "seen_px": None, "slip": None, "fee": 0.0,
                         "won": False, "pnl": 0.0, "reason": "",
                         "status": "unresolved"})
            continue
        won = up_won if f["side"] == "Up" else (not up_won)
        px = float(f["px"])
        sh = float(f["shares"])
        fee = float(f.get("fee_per_sh", FEE * px * (1 - px)))
        seen = (f.get("meta") or {}).get("seen_px")
        rows.append({"coin": coin,
                     "slug": f["slug"], "side": f["side"], "px": px,
                     "seen_px": seen,
                     "slip": (px - seen) if seen is not None else None,
                     "shares": sh, "fee": fee, "won": bool(won),
                     "pnl": sh * (float(won) - px - fee),
                     "reason": (f.get("meta") or {}).get("reason", "")[:60],
                     "status": "scored"})
    return rows


def _report(fills, a):
    if not fills:
        print("no latency-realistic fills yet on a verified-fresh oracle "
              "(use --all-latency / --all-oracle to see the earlier history)"
              if not a.all_oracle else
              f"no fills matching reason_prefix={a.reason_prefix!r}"
              + (f" since {a.since}" if a.since else ""))
        return
    tags = []
    tags.append("fresh-oracle" if not a.all_oracle else "ALL oracle states")
    tags.append("latency-realistic" if not a.all_latency
                else "ALL fills incl. zero-latency")
    print(f"({' + '.join(tags)}; --all-oracle / --all-latency to widen)")
    print(f"(filtered to reason_prefix={a.reason_prefix!r}"
          + (f", since {a.since}" if a.since else "") + ")")
    d = pd.DataFrame(score_rows(fills))
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
        print(f"  t = {sc.pnl.mean()/se:+.2f} on {len(sc)} fills "
              f"<- WRONG, assumes fills are independent")
    # Every Up fill in one market wins or loses together: they settle on one
    # outcome. 154 fills over 29 markets is 29 bets, not 154, and a run of
    # 154/154 is really 29/29 -- about a 1-in-10 event at p=0.93, not the
    # 1-in-100,000 the fill count suggests. Cluster on the slug.
    g = sc.groupby("slug").agg(pnl=("pnl", "sum"), sh=("shares", "sum"))
    if len(g) > 2:
        mu = g.pnl.sum() / g.sh.sum()
        resid = g.pnl - mu * g.sh
        se = (resid ** 2).sum() ** 0.5 / g.sh.sum()
        print(f"  t = {mu/se:+.2f} clustered by market (n={len(g)} markets)"
              f"   <- the honest one")
    mw = sc.groupby("slug").apply(
        lambda x: (x.won * x.shares).sum() / x.shares.sum() > 0.5,
        include_groups=False)
    print(f"  markets won {int(mw.sum())}/{len(mw)}")
    if sc.coin.nunique() > 1:
        print("\nby coin:")
        print(sc.groupby("coin").apply(lambda x: pd.Series({
            "fills": len(x), "shares": x.shares.sum(),
            "mkts": x.slug.nunique(),
            "hit": (x.won * x.shares).sum() / x.shares.sum(),
            "c_per_sh": x.pnl.sum() / x.shares.sum() * 100,
            "pnl": x.pnl.sum()}), include_groups=False).to_string(
                float_format=lambda v: f"{v:,.2f}"))

    print("\nby fill price:")
    b = pd.cut(sc.px, [0, .3, .5, .7, .85, .92, .95, .98, 1.0])
    print(sc.groupby(b, observed=True).apply(lambda x: pd.Series({
        "fills": len(x), "shares": x.shares.sum(),
        "hit": (x.won * x.shares).sum() / x.shares.sum(),
        "c_per_sh": x.pnl.sum() / x.shares.sum() * 100,
        "pnl": x.pnl.sum()})).to_string(float_format=lambda v: f"{v:,.2f}"))
    sl = sc[sc.slip.notna()]
    if len(sl):
        w = sl.shares
        print(f"\nslippage (paid - seen): mean {(sl.slip*w).sum()/w.sum()*100:+.2f}c/share"
              f"   worse {100*(sl.slip > 1e-9).mean():.0f}% / same "
              f"{100*(sl.slip.abs() <= 1e-9).mean():.0f}% / better "
              f"{100*(sl.slip < -1e-9).mean():.0f}% of fills")
    per_mkt = sc.groupby("slug").shares.sum()
    print(f"\nmarkets traded {len(per_mkt)}   shares/market: "
          f"mean {per_mkt.mean():.0f} median {per_mkt.median():.0f} "
          f"max {per_mkt.max():.0f}")
    print("\nlast 8 fills:")
    print(sc.tail(8)[["slug", "side", "px", "shares", "won", "pnl"]]
          .to_string(index=False, float_format=lambda v: f"{v:,.3f}"))


if __name__ == "__main__":
    main()
