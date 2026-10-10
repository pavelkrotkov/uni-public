from __future__ import annotations

from identity_overrides import alias_variants, load_identity_overrides
from shared.math_utils import percentile_map


def test_identity_overrides_load() -> None:
    aliases, manual_uei = load_identity_overrides()

    assert aliases
    assert manual_uei
    assert "Boston University" in aliases
    assert "Boston University" in manual_uei
    assert all(isinstance(uei, str) and uei for uei in manual_uei.values())
    assert "Washington University in St. Louis" in alias_variants(
        "Washington University in St Louis"
    )


def test_percentile_map_handles_ties_and_reverse_order() -> None:
    assert percentile_map([10.0, 20.0, 20.0, 30.0]) == {
        30.0: 100.0,
        20.0: 50.0,
        10.0: 0.0,
    }
    assert percentile_map([10.0, 20.0, 20.0, 30.0], reverse=True) == {
        10.0: 100.0,
        20.0: 50.0,
        30.0: 0.0,
    }
