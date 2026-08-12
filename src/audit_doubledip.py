"""How much of the recorded paper P&L could be liquidity double-dipping?

The fix in 9fbc1bc adds a lifetime ledger: the paper broker can never claim
more than vol_participation of what PRINTED in a market, cumulatively. The
fills already on disk were taken WITHOUT that ledger, so the same displayed
ask could be re-eaten after every venue book refresh, each time justified
by the same prints re-entering the rolling 5s window.

This bounds the damage retroactively. A fill is flagged as a POSSIBLE
re-claim when the same (market, side) already filled at the same limit
within `--gap` seconds -- the signature of the double-dip (book restored,
same prints still in window). That is an UPPER bound: some flagged fills
were genuinely new liquidity that printed in between; the ledger would
have allowed those.

Scores both subsets against the venue's settled outcomes, so the output is
directly comparable to src/status.py:

    clean   c/share and P&L from first-claims only
    flagged c/share and P&L from possible re-claims

If `clean` still clears the tape benchmark, the record survives the fix.

    python3 src/audit_doubledip.py            # droplet, all coins
"""
import argparse
import sys
from collections import defaultdict

import pandas as pd

sys.path.insert(0, "src")
import score_paper as sp  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gap", type=float, default=5.0,
                    help="seconds; re-fill at the same limit within this of "
                         "a prior fill = possible re-claim (the tape cap "
                         "window the old code rolled)")
    ap.add_argument("--since", default=None)
    a2 = sp.parser().parse_args([])
    a2.since = a2.since or None
    args = ap.parse_args()
    a2.since = args.since
    fills = sp.gather(a2)
    if not fills:
        print("no fills")
        return
    rows = sp.score_rows(fills)
    d = pd.DataFrame([r for r in rows if r["status"] == "scored"])
    if not len(d):
        print("nothing scored")
        return
    d = d.sort_values("t_us").reset_index(drop=True)
    last = defaultdict(lambda: (-1e18, None))   # (slug, side) -> (t, px)
    flagged = []
    for i, r in d.iterrows():
        key = (r.slug, r.side)
        t_prev, px_prev = last[key]
        same_px = px_prev is not None and abs(r.px - px_prev) < 0.015
        flagged.append((r.t_us - t_prev) < args.gap * 1e6 and same_px)
        last[key] = (r.t_us, r.px)
    d["flagged"] = flagged

    def block(x, label):
        if not len(x):
            print(f"  {label:<8} none")
            return
        w = x.shares.sum()
        print(f"  {label:<8} {len(x):>5} fills {w:>10,.0f} sh  "
              f"avg px {(x.px * x.shares).sum() / w:.4f}  "
              f"hit {(x.won * x.shares).sum() / w:.3f}  "
              f"{x.pnl.sum() / w * 100:>+6.2f}c/sh  ${x.pnl.sum():>+10,.2f}")

    print(f"possible re-claims: same market+side+limit within "
          f"{args.gap:g}s of a prior fill (UPPER bound on the double-dip)\n")
    print("ALL COINS")
    block(d[~d.flagged], "clean")
    block(d[d.flagged], "flagged")
    for coin in sorted(d.coin.unique()):
        x = d[d.coin == coin]
        print(f"\n{coin}")
        block(x[~x.flagged], "clean")
        block(x[x.flagged], "flagged")
    fr = d[d.flagged].shares.sum() / d.shares.sum()
    print(f"\n{fr:.1%} of shares are possible re-claims. The lifetime "
          f"ledger (9fbc1bc) blocks these going forward; if `clean` still "
          f"clears the tape (~1.8c/sh Binance-z, higher on Chainlink-z), "
          f"the paper record survives the stricter model.")


if __name__ == "__main__":
    main()
