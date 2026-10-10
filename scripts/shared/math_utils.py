from __future__ import annotations

from collections.abc import Iterable, Sequence


def clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def weighted_mean(pairs: Iterable[tuple[float | None, float]]) -> float | None:
    numerator = 0.0
    denominator = 0.0
    for value, weight in pairs:
        if value is None or weight <= 0:
            continue
        numerator += value * weight
        denominator += weight
    if denominator == 0:
        return None
    return numerator / denominator


def linear_regression_slope(points: Sequence[tuple[int, float]]) -> float | None:
    if len(points) < 2:
        return None
    mean_x = sum(point[0] for point in points) / len(points)
    mean_y = sum(point[1] for point in points) / len(points)
    numerator = sum((point[0] - mean_x) * (point[1] - mean_y) for point in points)
    denominator = sum((point[0] - mean_x) ** 2 for point in points)
    if denominator == 0:
        return None
    return numerator / denominator


def percentile_map(values: Sequence[float], *, reverse: bool = False) -> dict[float, float]:
    if not values:
        return {}
    ordered = sorted(values, reverse=not reverse)
    size = len(ordered)
    mapping: dict[float, float] = {}

    index = 0
    while index < size:
        value = ordered[index]
        start_index = index
        while index < size and ordered[index] == value:
            index += 1
        average_rank = (start_index + index - 1) / 2.0
        if size == 1:
            mapping[value] = 100.0
        else:
            mapping[value] = 100.0 * (1.0 - average_rank / (size - 1))
    return mapping


def normalize_percentage(value: float | None) -> float | None:
    if value is None:
        return None
    if value > 1:
        return value / 100.0
    return value
