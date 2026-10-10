from __future__ import annotations

from shared.math_utils import (
    clamp,
    linear_regression_slope,
    normalize_percentage,
    weighted_mean,
)


def test_clamp_below_lower_returns_lower() -> None:
    assert clamp(-0.5) == 0.0


def test_clamp_above_upper_returns_upper() -> None:
    assert clamp(1.5) == 1.0


def test_clamp_inside_range_returns_value() -> None:
    assert clamp(0.5) == 0.5


def test_clamp_at_default_boundaries_returns_boundary() -> None:
    assert clamp(0.0) == 0.0
    assert clamp(1.0) == 1.0


def test_clamp_with_custom_bounds() -> None:
    assert clamp(15.0, lower=-10.0, upper=10.0) == 10.0
    assert clamp(-20.0, lower=-10.0, upper=10.0) == -10.0
    assert clamp(0.0, lower=-10.0, upper=10.0) == 0.0


def test_weighted_mean_empty_iterable_returns_none() -> None:
    assert weighted_mean([]) is None


def test_weighted_mean_all_none_values_returns_none() -> None:
    assert weighted_mean([(None, 1.0), (None, 2.0)]) is None


def test_weighted_mean_skips_zero_and_negative_weights() -> None:
    # Only the (10.0, 2.0) pair should contribute.
    assert weighted_mean([(10.0, 2.0), (100.0, 0.0), (50.0, -1.0)]) == 10.0


def test_weighted_mean_mixed_nones_are_ignored() -> None:
    # None entries skipped; weighted average of (4.0, 1.0) and (6.0, 3.0).
    assert weighted_mean([(4.0, 1.0), (None, 5.0), (6.0, 3.0)]) == (4.0 + 18.0) / 4.0


def test_weighted_mean_single_pair() -> None:
    assert weighted_mean([(7.5, 2.0)]) == 7.5


def test_linear_regression_slope_fewer_than_two_points_returns_none() -> None:
    assert linear_regression_slope([]) is None
    assert linear_regression_slope([(0, 1.0)]) is None


def test_linear_regression_slope_positive_collinear_points() -> None:
    points = [(0, 0.0), (1, 2.0), (2, 4.0), (3, 6.0)]
    assert linear_regression_slope(points) == 2.0


def test_linear_regression_slope_negative_collinear_points() -> None:
    points = [(0, 10.0), (1, 7.0), (2, 4.0), (3, 1.0)]
    assert linear_regression_slope(points) == -3.0


def test_linear_regression_slope_all_same_x_returns_none() -> None:
    assert linear_regression_slope([(5, 1.0), (5, 2.0), (5, 3.0)]) is None


def test_normalize_percentage_none_returns_none() -> None:
    assert normalize_percentage(None) is None


def test_normalize_percentage_value_le_one_is_passthrough() -> None:
    assert normalize_percentage(0.5) == 0.5
    assert normalize_percentage(0.0) == 0.0


def test_normalize_percentage_value_above_one_is_divided_by_100() -> None:
    assert normalize_percentage(42.0) == 0.42


def test_normalize_percentage_at_exactly_one_is_passthrough() -> None:
    assert normalize_percentage(1.0) == 1.0
