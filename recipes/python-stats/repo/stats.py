"""Small descriptive statistics helpers.

median(values)
    The middle value of the sorted values; for an even count, the mean of the
    two middle values. Raises ValueError("median of empty data") when empty.

percentile(values, p)
    Linear interpolation between closest ranks (the "C = 1" method used by
    NumPy's default): with the values sorted as x[0..n-1], the rank is
    r = p / 100 * (n - 1), and the result is x[floor(r)] plus the fraction
    of r times the gap to the next value. p must be within 0..100, otherwise
    ValueError("percentile out of range") is raised. Empty data raises
    ValueError("percentile of empty data").
"""

import math


def median(values):
    data = sorted(values)
    n = len(data)
    if n == 0:
        raise ValueError("median of empty data")
    mid = n // 2
    if n % 2:
        return data[mid]
    return (data[mid - 1] + data[mid]) / 2


def percentile(values, p):
    if not 0 <= p <= 100:
        raise ValueError("percentile out of range")
    data = sorted(values)
    if not data:
        raise ValueError("percentile of empty data")
    rank = p / 100 * (len(data) - 1)
    low = math.floor(rank)
    high = min(low + 1, len(data) - 1)
    return data[low] + (rank - low) * (data[high] - data[low])
