"""Realised P&L from the VENUE's cash record.

The bot's day_pnl is an in-memory accumulator fed by settle events, and
it has a hole a restart drives straight through. _replay_positions
deliberately skips markets that already settled (they cannot settle
twice), and _seed_day_pnl rebuilds the day by re-summing our own
"settled" lines -- but if the process was DOWN when a market resolved,
no settled line was ever written and that P&L is gone for good.

Observed 2026-08-22 after ~12 restarts in a session: day_pnl read +0.28
against a real -12.15, including a -12.70 loss that never reached the
accumulator at all. day_pnl is what the daily loss limit reads, so the
one rail meant to survive a bad run was measuring the wrong thing and
would not have fired.

Polymarket's activity feed is immune to all of that: it is the venue's
own record of cash in and out, and a restart cannot erase it.

    BUY     usdcSize = cash out, fee included
    SELL    usdcSize = cash in,  fee deducted
    REDEEM  usdcSize = cash in

Only SETTLED markets are counted. An open position has money out and
nothing back yet, which is not a loss, and counting it as one would trip
the stop on every position the bot holds.
"""
import json
import time
import urllib.request

API = "https://data-api.polymarket.com/activity"
GRACE_S = 180.0          # redemption lands a minute or two after t1
FAMILY_S = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}


def settle_ts(slug):
    """Settlement time from `<coin>-updown-<fam>-<t0>`, or None."""
    try:
        parts = slug.split("-")
        step = FAMILY_S.get(parts[-2])
        return int(parts[-1]) + step if step else None
    except (ValueError, IndexError):
        return None


def fetch_activity(wallet, since_ts, timeout=20, max_rows=1500):
    rows, offset = [], 0
    while offset < max_rows:
        url = f"{API}?user={wallet}&limit=100&offset={offset}"
        req = urllib.request.Request(url, headers={"User-Agent":
                                                   "Mozilla/5.0"})
        arr = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        if not arr:
            break
        rows.extend(arr)
        if min(int(x.get("timestamp") or 0) for x in arr) < since_ts:
            break
        offset += 100
    return [x for x in rows if int(x.get("timestamp") or 0) >= since_ts]


def by_market(rows):
    """slug -> {buy, sell, redeem, shares, prices, last_ts}."""
    out = {}
    for x in rows:
        slug = x.get("slug") or "?"
        d = out.setdefault(slug, {"buy": 0.0, "sell": 0.0, "redeem": 0.0,
                                  "sh": 0.0, "px": [], "t": 0})
        usd = float(x.get("usdcSize") or 0)
        d["t"] = max(d["t"], int(x.get("timestamp") or 0))
        if x.get("type") == "REDEEM":
            d["redeem"] += usd
        elif str(x.get("side")).upper() == "BUY":
            d["buy"] += usd
            d["sh"] += float(x.get("size") or 0)
            d["px"].append(float(x.get("price") or 0))
        elif str(x.get("side")).upper() == "SELL":
            d["sell"] += usd
    return out


def realised_since(wallet, since_ts, now=None):
    """(net_dollars, n_markets, n_open) over SETTLED markets, or None.

    None means "could not read the venue" -- callers must not treat that
    as zero. Silently reporting a flat day when the feed is unreachable
    is the same class of failure this module exists to close.
    """
    if not wallet:
        return None
    try:
        rows = fetch_activity(wallet, since_ts)
    except Exception:                                   # noqa: BLE001
        return None
    now = now or time.time()
    net, n, n_open = 0.0, 0, 0
    for slug, d in by_market(rows).items():
        t1 = settle_ts(slug)
        if t1 is None or now < t1 + GRACE_S:
            n_open += 1
            continue
        if d["buy"] <= 0:
            continue
        net += d["sell"] + d["redeem"] - d["buy"]
        n += 1
    return net, n, n_open


def utc_day_start(now=None):
    return int((now or time.time()) // 86400) * 86400
