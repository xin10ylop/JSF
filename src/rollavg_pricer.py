"""Pricer for the post-2026-08-07 btc-updown contract.

RULE (effective 2026-08-07, all coins: BTC/ETH/SOL/XRP/DOGE):
    Up  iff  mean(P over [T-w, T])  >=  mean(P over [0, w])
    w = 30s for the 5-minute markets, 60s for the 15-minute markets.

Before 2026-08-07 the contract was  P_T >= P_0  (two instants).
Confirmed empirically: on markets where the two rules disagree, the OLD
rule was correct 80.5% pre-change (n=200) and 49.1% post-change (n=55) --
i.e. the old rule stopped governing exactly at the changeover.

--------------------------------------------------------------------
MATHEMATICS
Let K = mean(P,[0,w]) (fully known once t >= w) and A = mean(P,[T-w,T]).
Driftless, sigma = dollar vol per sqrt(second).

Phase 2 -- w <= t <= T-w   (averaging window not yet started, s = T-w-t):
    A | F_t ~ N( P_t , sigma^2 * (s + w/3) )
    P(Up) = Phi( (P_t - K) / (sigma * sqrt(s + w/3)) )
  => EFFECTIVE remaining time is (T-t) - 2w/3, not (T-t).

Phase 3 -- T-w < t <= T   (inside the settlement average; R = int_{T-w}^t P):
    A | F_t ~ N( (R + P_t*(T-t))/w , sigma^2 * (T-t)^3 / (3 w^2) )
    P(Up) = Phi( [R + P_t*(T-t) - K*w] / (sigma * sqrt((T-t)^3 / 3)) )
  => remaining variance decays as (T-t)^3, not (T-t).

CONSEQUENCE (the tradeable part): inside the final w seconds the contract
is far more determined than a terminal-value pricer believes. Variance
ratio vs terminal = (T-t)^2 / (3 w^2). With 10s left on a 5m market
(w=30) that is 1/27 -- a terminal model quoting 0.75 should be ~0.99.
A last-second spike can no longer swing the outcome; it only nudges the
average. Every incumbent model on this venue (the Semenas paper's N(d2),
our own G(z), and any bot built before 2026-08-07) prices the old
contract and is therefore systematically wrong in the back of every
window.

Phase 1 (t < w, strike still forming) is deliberately not traded: K and A
are both random there. Use `strike_known()` to gate.
"""
import numpy as np
from scipy.stats import norm

W_BY_HORIZON = {"5m": 30.0, "15m": 60.0}


def strike_known(t, w):
    """Strike is only fully determined once the first w seconds elapse."""
    return np.asarray(t, dtype=float) >= w


def fair(P_t, K, R, T, t, w, sigma):
    """P(Up) for the rolling-average contract. Requires t >= w.

    P_t   : current price
    K     : strike = mean price over [0, w]
    R     : integral of price over [T-w, t] (price-seconds); 0 if t <= T-w
    T,t,w : window length, elapsed time, averaging length (seconds)
    sigma : dollar vol per sqrt(second)
    """
    P_t = np.asarray(P_t, float); K = np.asarray(K, float)
    R = np.asarray(R, float); t = np.asarray(t, float)
    rem = T - t
    sig = np.asarray(sigma, float)
    out = np.full(np.broadcast(P_t, t).shape, np.nan, dtype=float)

    pre = t <= (T - w)                      # phase 2
    if np.any(pre):
        s = np.maximum((T - w) - t, 0.0)
        sd = sig * np.sqrt(np.maximum(s + w / 3.0, 1e-12))
        out = np.where(pre, norm.cdf((P_t - K) / np.maximum(sd, 1e-12)), out)

    ins = (~pre) & (rem > 1e-9)             # phase 3
    if np.any(ins):
        sd = sig * np.sqrt(np.maximum(rem, 1e-12) ** 3 / 3.0)
        num = R + P_t * rem - K * w
        out = np.where(ins, norm.cdf(num / np.maximum(sd, 1e-12)), out)

    done = rem <= 1e-9
    if np.any(done):
        out = np.where(done, (R / w >= K).astype(float), out)
    return out


def terminal_fair(P_t, K, T, t, sigma):
    """The pre-2026-08-07 pricer, kept for divergence measurement."""
    rem = np.maximum(np.asarray(T, float) - np.asarray(t, float), 1e-9)
    sd = np.asarray(sigma, float) * np.sqrt(rem)
    return norm.cdf((np.asarray(P_t, float) - np.asarray(K, float))
                    / np.maximum(sd, 1e-12))


def effective_remaining(T, t, w):
    """Effective remaining time under the new rule (phase 2)."""
    return np.maximum((T - t) - 2.0 * w / 3.0, 0.0)
