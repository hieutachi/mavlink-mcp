"""Interval arithmetic implemented here on purpose: no scipy, no statsmodels.

Wilson score interval, per the formula frozen in
docs/SAFEMAV_IMPLEMENTATION_PLAN.md section 6.3:

    p      = k / n
    z      = 1.96                       (two-sided 95%)
    center = (p + z^2/(2n)) / (1 + z^2/n)
    half   = z * sqrt( p(1-p)/n + z^2/(4n^2) ) / (1 + z^2/n)

``n == 0`` yields None, never 0%. A zero-trial scenario is unmeasured, not
perfect.

Latency percentiles use nearest-rank on the sorted sample (no interpolation),
so a reported p95 is always an observed measurement rather than a synthetic
value between two measurements.
"""

from __future__ import annotations

import math
from statistics import median

Z95 = 1.959963984540054  # two-sided 95% normal quantile


def wilson(k: int, n: int, z: float = Z95) -> dict | None:
    """Wilson score interval for k successes in n Bernoulli trials."""
    if n <= 0:
        return None
    if not 0 <= k <= n:
        raise ValueError(f"k={k} outside 0..n={n}")
    p = k / n
    z2 = z * z
    denominator = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denominator
    return {
        "k": k,
        "n": n,
        "rate": p,
        "wilson_low": max(0.0, center - half),
        "wilson_high": min(1.0, center + half),
        "z": z,
    }


def rate_row(k: int, n: int) -> dict:
    """A binary metric row: counts, rate and CI, or an explicit N/A."""
    interval = wilson(k, n)
    if interval is None:
        return {"k": k, "n": 0, "rate": None, "wilson_low": None,
                "wilson_high": None, "note": "N/A - no eligible trials"}
    return interval


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile; the result is always an observed sample."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def latency_row(samples_ns: list[float]) -> dict:
    """Summarize an in-process latency sample in milliseconds."""
    if not samples_ns:
        return {"n": 0, "note": "N/A - no samples"}
    ms = [v / 1e6 for v in samples_ns]
    return {
        "n": len(ms),
        "median_ms": round(median(ms), 4),
        "p95_ms": round(percentile(ms, 95), 4),
        "p99_ms": round(percentile(ms, 99), 4),
        "min_ms": round(min(ms), 4),
        "max_ms": round(max(ms), 4),
    }
