"""Fair-value models for updown binaries and evaluation utilities."""
import numpy as np
from scipy.stats import norm, t as student_t

VOL_COLS = ["rv5m", "rv15m", "rv1h", "rv4h", "rv24h", "bpv15m",
            "ewma1m", "ewma5m", "ewma30m"]


def fair_value_gauss(spot, strike, var_rate, rem_s, scale=1.0):
    v = np.maximum(var_rate * scale * rem_s, 1e-12)
    sd = np.sqrt(v)
    d2 = (np.log(spot / strike) - 0.5 * v) / sd
    return norm.cdf(d2)


def fair_value_t(spot, strike, var_rate, rem_s, nu=4.0, scale=1.0):
    v = np.maximum(var_rate * scale * rem_s, 1e-12)
    sd = np.sqrt(v * (nu - 2.0) / nu)
    z = (np.log(strike / spot) + 0.5 * v) / sd
    return 1.0 - student_t.cdf(z, df=nu)


def brier(p, y):
    p = np.asarray(p, dtype=float)
    y = np.asarray(y, dtype=float)
    ok = ~np.isnan(p)
    return np.mean((p[ok] - y[ok]) ** 2), ok.sum()


def log_score(p, y, eps=1e-6):
    p = np.clip(np.asarray(p, dtype=float), eps, 1 - eps)
    y = np.asarray(y, dtype=float)
    ok = ~np.isnan(p)
    return -np.mean(y[ok] * np.log(p[ok]) + (1 - y[ok]) * np.log(1 - p[ok])), ok.sum()


def calib_scale(spot, strike, settle, var_rate, rem_s):
    """Multiplicative variance calibration fitted on train data."""
    y = np.log(settle / spot) ** 2
    x = var_rate * rem_s
    ok = ~(np.isnan(y) | np.isnan(x)) & (x > 0)
    y, x = y[ok], x[ok]
    q = np.quantile(y, 0.995)
    m = y < q
    return float(np.sum(y[m]) / np.sum(x[m]))
