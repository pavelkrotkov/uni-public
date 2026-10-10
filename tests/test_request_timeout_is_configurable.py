"""Every loader's --request-timeout must actually take effect.

Raised twice in review of #55: a per-call `timeout=` literal silently overrides
the adapter's configured timeout, so the advertised CLI flag does nothing for
that call. The literal is invisible at the loader class, which is why it slipped
through once for BigQuery and again for two of harvest_ueis' three phases.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from typing import Any

import pytest
from shared import ROOT
from shared.data_loader import DataLoader

SCRIPTS = ROOT / "scripts"

# Loader modules that declare requires_json_source, as (module, class) pairs.
FETCHING_LOADERS = [
    ("load_nih_awards", "NIHAwardsLoader"),
    ("load_nsf_awards", "NSFAwardsLoader"),
    ("load_usaspending_totals", "USASpendingTotalsLoader"),
    ("load_ipeds_completions", "LoadIpedsCompletionsLoader"),
    ("load_scorecard_institution", "LoadScorecardInstitutionLoader"),
    ("load_scorecard_field_outcomes", "LoadScorecardFieldOutcomesLoader"),
    ("resolve_scorecard_ids", "ResolveScorecardIdsLoader"),
    ("harvest_ueis", "HarvestUeisLoader"),
    ("add_uei_duns", "AddUeiDunsLoader"),
    ("backfill_university_metadata", "BackfillUniversityMetadataLoader"),
    ("extract_openalex_metrics", "ExtractOpenAlexMetricsLoader"),
    ("load_university_cip_offers", "LoadUniversityCipOffersLoader"),
    ("bootstrap_db", "BootstrapDbLoader"),
]


def _loader(module_name: str, class_name: str) -> DataLoader:
    module = importlib.import_module(module_name)
    return getattr(module, class_name)()


@pytest.mark.parametrize(("module_name", "class_name"), FETCHING_LOADERS)
def test_loader_declares_the_json_source_seam(module_name: str, class_name: str) -> None:
    loader = _loader(module_name, class_name)
    assert loader.requires_json_source is True


@pytest.mark.parametrize(("module_name", "class_name"), FETCHING_LOADERS)
def test_request_timeout_flag_reaches_the_adapter(module_name: str, class_name: str) -> None:
    loader = _loader(module_name, class_name)
    adapter: Any = loader.build_json_source(loader._parse_args(["--request-timeout", "7"]))
    assert adapter.timeout == 7


@pytest.mark.parametrize(("module_name", "class_name"), FETCHING_LOADERS)
def test_max_attempts_flag_reaches_the_adapter(module_name: str, class_name: str) -> None:
    loader = _loader(module_name, class_name)
    adapter: Any = loader.build_json_source(loader._parse_args(["--max-attempts", "3"]))
    assert adapter.max_attempts == 3


def _fetch_calls_with_literal_timeout(path: Path) -> list[int]:
    """Line numbers of `.fetch(..., timeout=<literal>)` calls in ``path``."""
    tree = ast.parse(path.read_text())
    offenders: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "fetch":
            continue
        for keyword in node.keywords:
            if keyword.arg == "timeout" and isinstance(keyword.value, ast.Constant):
                offenders.append(node.lineno)
    return offenders


@pytest.mark.parametrize("path", sorted(SCRIPTS.rglob("*.py")), ids=lambda p: p.name)
def test_no_script_pins_a_literal_timeout_on_a_fetch(path: Path) -> None:
    """Set `default_request_timeout` on the loader instead of a per-call literal.

    A literal wins over the adapter, so it silently disables --request-timeout
    for that call. A timeout is a ceiling rather than a wait, so one loader-level
    default sized for the slowest call serves every call in that loader.
    """
    offenders = _fetch_calls_with_literal_timeout(path)
    assert not offenders, (
        f"{path.relative_to(ROOT)} pins a literal timeout on .fetch() at line(s) "
        f"{offenders}; set default_request_timeout on the loader class instead"
    )
