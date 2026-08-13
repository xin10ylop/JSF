"""Score fills by the oracle's age at signal time: the retune's own trial.

The stale-spot guard was loosened 3.5s -> 8.0s on live evidence (commit
5354212). Every fill is stamped with oracle_age_s at signal time, so the
record itself can prove or refute the retune: if the (3.5, 8] bucket --
the region the retune released -- earns, the freed volume was real EV; if
it bleeds, the guard gets re-tightened to exactly where the data says.

    python3 src/age_report.py
"""
import glob
import json
import sys

import pandas as pd
import requests

sys.path.insert(0, "src")
import score_paper as sp  # noqa: E402


def main():
    fills = []
    for p in sorted(glob.glob("logs/*/paper_fills.jsonl")):
        coin = p.split("/")[1]
        for line in open(p):
            try:
                d = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if d.get("kind") != "taker_fill":
                continue
            m = d.get("meta") or {}
            if m.get("seen_px") is None or m.get("oracle_age_s") is None:
                continue
            d["coin"] = coin
            fills.append(d)
    if not fills:
        print("no stamped fills yet")
        return
    s = requests.Session()
    s.headers.update(sp.UA)
    res, _failed = sp.outcomes_for([f["slug"] for f in fills], s)
    rows = []
    for f in fills:
        w = res.get(f["slug"])
        if w is None:
            continue
        won = w if f["side"] == "Up" else (not w)
        px, sh = float(f["px"]), float(f["shares"])
        fee = float(f.get("fee_per_sh", 0.0))
        rows.append({"age": float(f["meta"]["oracle_age_s"]),
                     "pnl": sh * (float(won) - px - fee), "sh": sh,
                     "won": float(won) * sh, "slug": f["slug"],
                     "coin": f["coin"]})
    d = pd.DataFrame(rows)
    print(f"{len(d):,} scored stamped fills, {d.slug.nunique()} markets\n")
    d["bucket"] = pd.cut(d.age, [0, 2.0, 3.5, 8.0, 100.0])
    t = d.groupby("bucket", observed=True).apply(lambda x: pd.Series({
        "fills": len(x), "shares": x.sh.sum(),
        "mkts": x.slug.nunique(),
        "hit": x.won.sum() / x.sh.sum(),
        "c_per_sh": 100 * x.pnl.sum() / x.sh.sum(),
        "pnl": x.pnl.sum()}), include_groups=False)
    print(t.to_string(float_format=lambda v: f"{v:,.2f}"))
    print("\n  (0, 2]   = fresh spot (Binance-led or a just-arrived round)")
    print("  (2, 3.5] = routine delivery lag, was always allowed")
    print("  (3.5, 8] = THE RELEASED REGION -- this row judges the retune")


if __name__ == "__main__":
    main()
