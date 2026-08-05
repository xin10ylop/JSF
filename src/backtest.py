"""Execution simulators for Polymarket binaries.

Maker fill rule (conservative, evidence-based):
- A limit BUY at level L posted at t is considered filled by a later
  sell-aggressor print at price p < L (price priority proves the book at L
  was consumed), for the full remaining size.
- Prints exactly AT L fill only the part of the print size beyond the
  queue ahead of us (queue_ahead shares posted before us at L, consumed
  first; queue_ahead is an input, from book data or a calibrated typical).
- No fills after cancel time. Winning shares redeem $1 at settlement.

Taker rule: buy at the prevailing ask (tape-implied or book), pay the
0.07*p*(1-p) fee, optional +1 tick slippage stress.

Capacity accounting: every fill is capped by actual printed volume at or
through the level - we never assume liquidity that did not trade.
"""
import numpy as np

FEE = 0.07


def maker_fills(tape_ts, tape_px, tape_is_buy, tape_sz,
                t_post, level, size, t_cancel, queue_ahead=0.0):
    """Simulate one maker BUY order on the (Up-token) tape.

    Returns (filled_shares, avg_price, t_first_fill_us).
    Sell-aggressor prints (is_buy=False) hit bids.
    """
    i0 = np.searchsorted(tape_ts, t_post, side="right")
    i1 = np.searchsorted(tape_ts, t_cancel, side="right")
    filled = 0.0
    t_first = -1
    q = queue_ahead
    for i in range(i0, i1):
        if tape_is_buy[i]:
            continue
        p = tape_px[i]
        s = tape_sz[i]
        if p < level - 1e-9:
            take = min(size - filled, s)
            if take > 0:
                filled += take
                if t_first < 0:
                    t_first = tape_ts[i]
        elif abs(p - level) <= 1e-9:
            beyond = max(0.0, s - q)
            q = max(0.0, q - s)
            take = min(size - filled, beyond)
            if take > 0:
                filled += take
                if t_first < 0:
                    t_first = tape_ts[i]
        if filled >= size - 1e-9:
            break
    return filled, level, t_first


def maker_pnl(filled, level, won, credit_rebate=0.0):
    """P&L in $ for a filled maker buy held to settlement."""
    payoff = 1.0 if won else 0.0
    return filled * (payoff - level + credit_rebate)


def taker_pnl(entry_px, shares, won, extra_slip=0.0):
    px = entry_px + extra_slip
    fee = FEE * px * (1 - px)
    payoff = 1.0 if won else 0.0
    return shares * (payoff - px - fee)
