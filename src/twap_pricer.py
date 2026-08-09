"""Pricing for average-price (TWAP / Asian) binaries — built for the
2026-08-07 rule-wording change on btc-updown markets.

Contract: Up iff mean(S_u, u in [0,T]) >= K, where K = S_0.

At time t with running integral A_t = int_0^t S_u du known:
    remaining integral ~ Normal( S_t*(T-t), sigma_$^2 * (T-t)^3 / 3 )
    z = [A_t + S_t*(T-t) - K*T] / (sigma_$ * sqrt((T-t)^3/3))
    P(Up) = Phi(z)

Two properties make this contract behave very differently from the
terminal-value binary the venue used before 2026-08-07:

1. Total variance is sigma^2*T/3, i.e. effective vol is sigma/sqrt(3).
2. Remaining uncertainty decays as (T-t)^1.5, not (T-t)^0.5. With 10% of
   the window left an average-price binary is ~10x more determined than a
   terminal one. Measured on real BTC paths (Jun-Jul 2026), at 95% through
   a 15m window the correct model scores Brier 0.0021 while a terminal
   model scores 0.108 and the market historically scored 0.042.

Therefore: IF the venue settles on an average, anyone still pricing
terminal value is systematically and largely wrong in the back half of
every window. Verification of the actual settlement rule is a hard
prerequisite — see src/verify_settlement_rule.py.
"""
import numpy as np
from scipy.stats import norm


def twap_fair(A_t, S_t, K, T, t, sigma_dollar_per_sqrt_s):
    """P(mean price over [0,T] >= K) given the path so far.

    A_t : running integral of price over [0, t]   (price * seconds)
    S_t : current price
    K   : strike (price at window open)
    T,t : window length and elapsed time, seconds
    sigma_dollar_per_sqrt_s : dollar vol per sqrt(second)
    """
    rem = np.asarray(T - t, dtype=float)
    out = np.full(np.shape(rem), np.nan, dtype=float)
    live = rem > 1e-9
    if np.any(~live):
        done = (np.asarray(A_t, dtype=float) / max(T, 1e-9)) >= K
        out = np.where(live, out, done.astype(float))
    sd = np.asarray(sigma_dollar_per_sqrt_s, dtype=float) * np.sqrt(
        np.maximum(rem, 1e-9) ** 3 / 3.0)
    num = np.asarray(A_t, dtype=float) + np.asarray(S_t, dtype=float) * rem \
        - np.asarray(K, dtype=float) * T
    z = np.where(sd > 0, num / np.maximum(sd, 1e-12), 0.0)
    return np.where(live, norm.cdf(z), out)


def terminal_fair(S_t, K, T, t, sigma_dollar_per_sqrt_s):
    """P(S_T >= K) — the pre-2026-08-07 contract, for comparison."""
    rem = np.maximum(np.asarray(T - t, dtype=float), 1e-9)
    sd = np.asarray(sigma_dollar_per_sqrt_s, dtype=float) * np.sqrt(rem)
    return norm.cdf((np.asarray(S_t, dtype=float)
                     - np.asarray(K, dtype=float)) / np.maximum(sd, 1e-12))


def running_integral(prices_1s, i0, i1):
    """Integral of a 1s price series over [i0, i1) in price-seconds."""
    c = np.concatenate([[0.0], np.cumsum(prices_1s)])
    return c[i1] - c[i0]
