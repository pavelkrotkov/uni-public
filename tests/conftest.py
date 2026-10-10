from __future__ import annotations

import pytest
from identity_overrides import get_identity_overrides


@pytest.fixture(autouse=True)
def _clear_identity_overrides_cache() -> None:
    get_identity_overrides.cache_clear()
