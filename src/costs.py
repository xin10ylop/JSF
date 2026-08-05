"""Polymarket cost model — verified against docs.polymarket.com (2026-08-05).

Taker fee: fee = shares * FEE_RATE_CRYPTO * p * (1-p), charged in USDC on
every taker fill in crypto markets. Makers pay zero; maker rebates
(15-25% of pooled taker fees) are NOT credited here (conservative).

No deposit/withdrawal/redemption fees. Winning shares redeem $1.
"""

FEE_RATE_CRYPTO = 0.07


def taker_fee_per_share(price: float, fee_rate: float = FEE_RATE_CRYPTO) -> float:
    return fee_rate * price * (1.0 - price)


def taker_cost(price: float, shares: float) -> float:
    return shares * taker_fee_per_share(price)


def maker_fee_per_share(price: float) -> float:  # noqa: ARG001
    return 0.0


def breakeven_win_prob_taker(price: float) -> float:
    return price + taker_fee_per_share(price)


def breakeven_win_prob_maker(price: float) -> float:
    return price
