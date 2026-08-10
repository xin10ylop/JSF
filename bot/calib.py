"""Empirical z -> P(Up), measured rather than assumed.

The Gaussian model says P(Up) = Phi(z). It is badly overconfident at this
horizon: measured on 122,709 real prints across five coins in the settle
window of post-2026-08-07 markets, z > 3 settles Up 91.6% of the time, not
the 99.87% Phi(3) claims. Fat tails, the Chainlink-vs-Binance basis and
oracle timing all eat into it.

Using Phi(z) as the bot's fair value makes the `fair - ask >= edge_min`
test vacuous (Phi(z) saturates at 1.0 the moment z clears the gate), so the
bot would take any ask below 0.98 with no real edge check. This table makes
the fair value honest, so edge_min binds on something real.

Source: src/endgame_rollavg.py calibration output, 2026-08-07..09.
Bucket midpoints, monotone, linearly interpolated and clamped.
"""
import bisect

# (z, P(Up)) at bucket midpoints from the measured calibration table
POINTS = [
    (-3.5, 0.1833),
    (-2.5, 0.1917),
    (-1.5, 0.2860),
    (-0.75, 0.2917),
    (-0.25, 0.4524),
    (0.25, 0.5646),
    (0.75, 0.7901),
    (1.5, 0.8382),
    (2.5, 0.8878),
    (3.5, 0.9159),
]
_Z = [p[0] for p in POINTS]
_P = [p[1] for p in POINTS]


def p_up(z):
    """Empirical P(Up) for a signed z. Flat outside the measured range."""
    if z is None:
        return None
    if z <= _Z[0]:
        return _P[0]
    if z >= _Z[-1]:
        return _P[-1]
    i = bisect.bisect_right(_Z, z)
    z0, z1 = _Z[i - 1], _Z[i]
    p0, p1 = _P[i - 1], _P[i]
    return p0 + (p1 - p0) * (z - z0) / (z1 - z0)
