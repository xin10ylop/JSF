"""Pricer for the post-2026-08-07 btc-updown contract.

RULE (effective 2026-08-07, all coins: BTC/ETH/SOL/XRP/DOGE):
    Up  iff  mean(P over [T-w, T))  >=  mean(P over [-w, 0))
    w = 30s. BOTH averages are TRAILING: the strike is the w seconds
    BEFORE the window opens, not the first w seconds inside it. This is
    what a Chainlink `<coin>-usd-twap-30s-streams` feed sampled at the two
    boundaries produces, and it is what the venue metadata points at.

Verified on the settled outcomes of 2,880 post-change markets across five
coins (free Binance 1s klines as the price path):

    rule                       pre-change   post-change
    old  P(t1) >= P(t0)          0.9491       0.8904
    trailing / trailing          0.9129       0.9503   <-- governs after
    forward strike [0,w]         0.8990       0.8943

and on the 266 post-change markets where the trailing rule and the old
rule disagree, the trailing rule is correct 82.7% vs the old rule's 17.3%
(every coin individually 73-91%). A scan over w peaks exactly at 30s
(15s 0.9302, 20s 0.9344, 30s 0.9503, 45s 0.9399, 60s 0.9149).

NOTE: an earlier revision of this file assumed a FORWARD strike over
[0, w]. That is the row above that does not govern in either era; the
consequence is that K is known at t=0 and there is no dead zone at the
start of the window.

--------------------------------------------------------------------
MATHEMATICS
Let K = mean(P,[-w,0)) (known at t=0) and A = mean(P,[T-w,T)).
Driftless, sigma = dollar vol per sqrt(second).

Phase 2 -- 0 <= t <= T-w   (averaging window not yet started, s = T-w-t):
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

There is no phase 1: K is a backward-looking constant, so the contract is
priceable from t=0. `strike_known` is retained as a no-op for callers.
"""
import numpy as np
from scipy.stats import norm

W_BY_HORIZON = {"5m": 30.0, "15m": 60.0}


def strike_known(t, w=None):
    """Always true under the trailing-strike rule; K is set before t=0.

    Kept so existing callers keep working after the correction.
    """
    return np.ones_like(np.asarray(t, dtype=float), dtype=bool)


def fair(P_t, K, R, T, t, w, sigma):
    """P(Up) for the rolling-average contract.

    P_t   : current price
    K     : strike = mean price over [-w, 0), i.e. the w seconds
            BEFORE the window opened (known at t=0)
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
