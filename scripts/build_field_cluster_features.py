#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "PyYAML>=6.0",
#   "pyarrow>=20.0.0",
# ]
# ///

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shared.config import resolve_output_dir
from shared.data_loader import ManualDataLoader
from shared.feature_store import COLUMNS, validate_emitted_rows
from shared.io_utils import latest_openalex_run_dir, load_parquet_rows, read_json, write_parquet
from shared.math_utils import clamp, linear_regression_slope, weighted_mean

LOOKBACK_YEARS = 5
TOTAL_COMPLETIONS_CIP = "00.0099"
PREFERRED_COUNTING_METHODS = ("topic_deduped_subfield", "primary_subfield")


class FeatureConfigError(RuntimeError):
    """Raised when feature-builder config is invalid."""


@dataclass(frozen=True)
class ClusterDefinition:
    slug: str
    label: str
    scoring_profile: str
    strict_cips: tuple[str, ...]
    family_cips: tuple[str, ...]
    strict_cip4s: tuple[str, ...]
    family_cip4s: tuple[str, ...]
    strict_subfield_weights: dict[int, float]
    family_subfield_weights: dict[int, float]
    fit_weights: dict[str, float]
    strict_mapped_cips: tuple[str, ...]
    family_mapped_cips: tuple[str, ...]


@dataclass(frozen=True)
class FeatureConfig:
    clusters: tuple[ClusterDefinition, ...]
    research_start: int
    research_end: int
    min_recent_bachelors_in_target_family: int
    allow_institution_level_outcome_fallback: bool


@dataclass(frozen=True)
class OutcomeAggregate:
    available: bool
    earnings_1yr: float | None
    earnings_5yr: float | None
    debt: float | None
    completers: int
    row_count: int


@dataclass(frozen=True)
class FeatureArtifacts:
    universities: list[dict[str, Any]]
    institution_rows: dict[int, dict[str, Any]]
    field_rows: dict[int, list[dict[str, Any]]]
    completion_rows: dict[int, list[dict[str, Any]]]
    completion_lookback_years: list[int]
    research_rows: dict[int, list[dict[str, Any]]]
    openalex_run_dir: Path
    research_counting_method: str
    grant_rows: dict[int, dict[str, dict[str, float]]]
    offer_rows: dict[int, dict[str, tuple[int, int]]]


@dataclass(frozen=True)
class MergedClusterFeatures:
    """CIP→subfield join output for one (university, cluster).

    Bundles completion counts (CIP-keyed) with research metrics (subfield-keyed)
    that have been merged through the cluster's CIP→subfield weight mapping.
    """

    university_id: int
    cluster_slug: str
    strict_offers: tuple[str, ...]
    family_offers: tuple[str, ...]
    strict_unmapped_offers: tuple[str, ...]
    family_unmapped_offers: tuple[str, ...]
    strict_completions: int
    strict_completions_by_year: dict[int, int]
    family_completions: int
    family_completions_by_year: dict[int, int]
    total_bachelors: int
    total_bachelors_by_year: dict[int, int]
    strict_persistence_years: int
    family_persistence_years: int
    strict_research: dict[str, Any]
    family_research: dict[str, Any]


@dataclass(frozen=True)
class MergedFeatureData:
    rows: tuple[MergedClusterFeatures, ...]
    research_years: tuple[int, ...]
    lookback_years: tuple[int, ...]


@dataclass(frozen=True)
class ClusterScore:
    """Per-(university, cluster) scores derivable from MergedFeatureData.

    Confidence components that depend on inputs outside MergedFeatureData —
    research grants, Scorecard outcomes, identifier flags — are computed
    by the caller and merged with these rows downstream.
    """

    university_id: int
    cluster_slug: str
    target_completions_share_direct: float | None
    target_completions_share_family: float | None
    confidence_program: float
    confidence_mapping: float
    thin_completion_flag: int
    sparse_mapping_flag: int
    offers_any_target_cip: int
    min_recent_bachelors_threshold_passed: int
    eligible_for_ranking: int
    exclusion_reasons: tuple[str, ...]


@dataclass(frozen=True)
class ScoredFeatures:
    rows: tuple[ClusterScore, ...]


class FeatureArtifactError(RuntimeError):
    """Raised when a required feature-builder artifact is missing or unreadable."""


def resolve_cluster_cips(conn, rules: dict[str, Any]) -> tuple[list[str], list[str]]:
    strict = set(rules.get("include_6digit", []))
    family = set(strict)
    families = rules.get("include_2digit_families", [])
    if families:
        placeholders = ",".join("?" for _ in families)
        rows = conn.execute(
            f"""
            SELECT cip_code
            FROM cips
            WHERE cip_family_code IN ({placeholders})
            """,
            list(families),
        ).fetchall()
        family.update(row["cip_code"] for row in rows)
    for excluded in rules.get("exclude_6digit", []):
        strict.discard(excluded)
        family.discard(excluded)
    return sorted(strict), sorted(family)


def normalize_subfield_weights(
    conn, cip_codes: list[str], fit_weights: dict[str, float]
) -> tuple[dict[int, float], list[str]]:
    if not cip_codes:
        return {}, []
    placeholders = ",".join("?" for _ in cip_codes)
    rows = conn.execute(
        f"""
        SELECT cip_code, fit_level, subfield_id
        FROM cip_trains_subfield
        WHERE cip_code IN ({placeholders})
        """,
        cip_codes,
    ).fetchall()
    subfield_weights: dict[int, float] = {}
    mapped_cips = {row["cip_code"] for row in rows}
    for row in rows:
        weight = float(fit_weights.get(row["fit_level"], 0.0))
        current = subfield_weights.get(row["subfield_id"], 0.0)
        if weight > current:
            subfield_weights[row["subfield_id"]] = weight
    return subfield_weights, sorted(mapped_cips)


def _config_error(path: str, message: str) -> FeatureConfigError:
    return FeatureConfigError(f"{path}: {message}")


def _require_mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _config_error(path, "expected mapping")
    return value


def _require_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise _config_error(path, "expected list")
    return value


def _require_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _config_error(path, "expected integer")
    return value


def _require_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise _config_error(path, "expected boolean")
    return value


def _require_nonempty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _config_error(path, "expected non-empty string")
    return value


def _string_list(value: Any, path: str) -> list[str]:
    items = _require_list(value, path)
    for index, item in enumerate(items):
        _require_nonempty_string(item, f"{path}[{index}]")
    return list(items)


def _known_cip_codes(conn: sqlite3.Connection, cip_codes: set[str]) -> set[str]:
    if not cip_codes:
        return set()
    placeholders = ",".join("?" for _ in cip_codes)
    rows = conn.execute(
        f"SELECT cip_code FROM cips WHERE cip_code IN ({placeholders})",
        sorted(cip_codes),
    ).fetchall()
    return {str(row["cip_code"]) for row in rows}


def _known_cip_family_codes(conn: sqlite3.Connection, family_codes: set[str]) -> set[str]:
    if not family_codes:
        return set()
    placeholders = ",".join("?" for _ in family_codes)
    rows = conn.execute(
        f"SELECT DISTINCT cip_family_code FROM cips WHERE cip_family_code IN ({placeholders})",
        sorted(family_codes),
    ).fetchall()
    return {str(row["cip_family_code"]) for row in rows}


def _known_fit_levels(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT fit_level
        FROM cip_trains_subfield
        WHERE fit_level IS NOT NULL
        """
    ).fetchall()
    return {str(row["fit_level"]) for row in rows}


def _validate_cip_rules(
    conn: sqlite3.Connection,
    rules: dict[str, Any],
    path: str,
) -> dict[str, list[str]]:
    include_6digit = _string_list(rules.get("include_6digit", []), f"{path}.include_6digit")
    include_2digit_families = _string_list(
        rules.get("include_2digit_families", []),
        f"{path}.include_2digit_families",
    )
    exclude_6digit = _string_list(rules.get("exclude_6digit", []), f"{path}.exclude_6digit")

    configured_cips = set(include_6digit) | set(exclude_6digit)
    unknown_cips = sorted(configured_cips - _known_cip_codes(conn, configured_cips))
    if unknown_cips:
        raise _config_error(path, f"unknown CIP codes: {', '.join(unknown_cips)}")

    configured_families = set(include_2digit_families)
    unknown_families = sorted(
        configured_families - _known_cip_family_codes(conn, configured_families)
    )
    if unknown_families:
        raise _config_error(path, f"unknown CIP family codes: {', '.join(unknown_families)}")

    return {
        "include_6digit": include_6digit,
        "include_2digit_families": include_2digit_families,
        "exclude_6digit": exclude_6digit,
    }


def _validate_fit_weights(
    fit_weights: dict[str, Any],
    path: str,
    known_fit_levels: set[str],
) -> dict[str, float]:
    if not fit_weights:
        raise _config_error(path, "must contain at least one fit level weight")

    for key in fit_weights:
        if not isinstance(key, str) or not key.strip():
            raise _config_error(path, "fit level names must be non-empty strings")

    unknown_levels = sorted(set(fit_weights) - known_fit_levels)
    if unknown_levels:
        raise _config_error(path, f"unknown fit levels: {', '.join(unknown_levels)}")

    normalized: dict[str, float] = {}
    for key, value in fit_weights.items():
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise _config_error(f"{path}.{key}", "expected numeric weight")
        weight = float(value)
        if weight < 0:
            raise _config_error(f"{path}.{key}", "expected non-negative weight")
        normalized[str(key)] = weight
    if not any(weight > 0 for weight in normalized.values()):
        raise _config_error(path, "at least one weight must be greater than zero")
    return normalized


def _validate_cluster_payload(
    conn: sqlite3.Connection,
    cluster: Any,
    index: int,
    scoring_profiles: set[str],
    known_fit_levels: set[str],
) -> dict[str, Any]:
    path = f"field_clusters[{index}]"
    payload = _require_mapping(cluster, path)
    missing = sorted(
        key for key in ("slug", "label", "cip_rules", "fit_weights") if key not in payload
    )
    if missing:
        raise _config_error(path, f"missing required keys: {', '.join(missing)}")

    slug = _require_nonempty_string(payload["slug"], f"{path}.slug")
    label = _require_nonempty_string(payload["label"], f"{path}.label")
    scoring_profile = _require_nonempty_string(
        payload.get("scoring_profile", "balanced"),
        f"{path}.scoring_profile",
    )
    if scoring_profile not in scoring_profiles:
        raise _config_error(
            f"{path}.scoring_profile",
            f"unknown scoring profile: {scoring_profile}",
        )

    rules = _validate_cip_rules(
        conn,
        _require_mapping(payload["cip_rules"], f"{path}.cip_rules"),
        f"{path}.cip_rules",
    )
    fit_weights = _validate_fit_weights(
        _require_mapping(payload["fit_weights"], f"{path}.fit_weights"),
        f"{path}.fit_weights",
        known_fit_levels,
    )

    return {
        "slug": slug,
        "label": label,
        "scoring_profile": scoring_profile,
        "cip_rules": rules,
        "fit_weights": fit_weights,
    }


def _build_cluster_definition(
    conn: sqlite3.Connection,
    cluster: dict[str, Any],
    index: int,
) -> ClusterDefinition:
    strict_cips, family_cips = resolve_cluster_cips(conn, cluster["cip_rules"])
    if not strict_cips and not family_cips:
        raise _config_error(
            f"field_clusters[{index}].cip_rules",
            "resolved to zero CIP codes; check include_6digit/include_2digit_families/exclude_6digit",
        )
    fit_weights = cluster["fit_weights"]
    strict_subfield_weights, strict_mapped_cips = normalize_subfield_weights(
        conn, strict_cips, fit_weights
    )
    family_subfield_weights, family_mapped_cips = normalize_subfield_weights(
        conn, family_cips, fit_weights
    )
    return ClusterDefinition(
        slug=cluster["slug"],
        label=cluster["label"],
        scoring_profile=cluster["scoring_profile"],
        strict_cips=tuple(strict_cips),
        family_cips=tuple(family_cips),
        strict_cip4s=tuple(sorted({cip.replace(".", "")[:4] for cip in strict_cips})),
        family_cip4s=tuple(sorted({cip.replace(".", "")[:4] for cip in family_cips})),
        strict_subfield_weights=strict_subfield_weights,
        family_subfield_weights=family_subfield_weights,
        fit_weights=fit_weights,
        strict_mapped_cips=tuple(strict_mapped_cips),
        family_mapped_cips=tuple(family_mapped_cips),
    )


def validate_feature_config(config: dict[str, Any], conn: sqlite3.Connection) -> FeatureConfig:
    """Validate and normalize feature-builder config."""

    payload = _require_mapping(config, "config")
    field_clusters = _require_list(payload.get("field_clusters"), "field_clusters")
    if not field_clusters:
        raise _config_error("field_clusters", "must contain at least one cluster")

    years = _require_mapping(payload.get("years"), "years")
    research_start = _require_int(years.get("research_start"), "years.research_start")
    research_end = _require_int(years.get("research_end"), "years.research_end")
    if research_start > research_end:
        raise _config_error("years", "research_start must be <= research_end")

    institution_filters = _require_mapping(
        payload.get("institution_filters"),
        "institution_filters",
    )
    threshold = _require_int(
        institution_filters.get("min_recent_bachelors_in_target_family"),
        "institution_filters.min_recent_bachelors_in_target_family",
    )
    if threshold < 0:
        raise _config_error(
            "institution_filters.min_recent_bachelors_in_target_family",
            "expected non-negative integer",
        )

    feature_flags = _require_mapping(payload.get("feature_flags"), "feature_flags")
    allow_fallback = _require_bool(
        feature_flags.get("allow_institution_level_outcome_fallback"),
        "feature_flags.allow_institution_level_outcome_fallback",
    )

    scoring_profiles = _require_mapping(payload.get("scoring_profiles"), "scoring_profiles")
    if not scoring_profiles:
        raise _config_error("scoring_profiles", "must contain at least one profile")
    profile_names = set(scoring_profiles)
    known_fit_levels = _known_fit_levels(conn)
    seen_slugs: set[str] = set()
    normalized_clusters: list[ClusterDefinition] = []
    for index, cluster in enumerate(field_clusters):
        normalized = _validate_cluster_payload(
            conn,
            cluster,
            index,
            profile_names,
            known_fit_levels,
        )
        slug = normalized["slug"]
        if slug in seen_slugs:
            raise _config_error(f"field_clusters[{index}].slug", f"duplicate slug: {slug}")
        seen_slugs.add(slug)
        normalized_clusters.append(_build_cluster_definition(conn, normalized, index))

    return FeatureConfig(
        clusters=tuple(normalized_clusters),
        research_start=research_start,
        research_end=research_end,
        min_recent_bachelors_in_target_family=threshold,
        allow_institution_level_outcome_fallback=allow_fallback,
    )


def load_universities(conn) -> list[dict[str, Any]]:
    university_rows = conn.execute(
        """
        SELECT university_id, canonical_name, state_code, city, control, country_code
        FROM universities
        ORDER BY university_id
        """
    ).fetchall()
    identifier_rows = conn.execute(
        """
        SELECT university_id, system_code, external_id, match_status
        FROM university_identifiers
        WHERE system_code IN ('OPENALEX', 'SCORECARD_UNITID', 'SCORECARD_OPEID6', 'UEI')
        """
    ).fetchall()
    by_university: dict[int, dict[str, Any]] = defaultdict(dict)
    for row in identifier_rows:
        by_university[row["university_id"]][row["system_code"]] = {
            "external_id": row["external_id"],
            "match_status": row["match_status"],
        }

    universities = []
    for row in university_rows:
        identifiers = by_university.get(row["university_id"], {})
        uei_info = identifiers.get("UEI", {})
        uei_match_status = uei_info.get("match_status") if uei_info else None
        universities.append(
            {
                "university_id": row["university_id"],
                "canonical_name": row["canonical_name"],
                "state_code": row["state_code"],
                "city": row["city"],
                "control": row["control"],
                "country_code": row["country_code"],
                "has_openalex_id": int("OPENALEX" in identifiers),
                "has_scorecard_unitid": int("SCORECARD_UNITID" in identifiers),
                "has_scorecard_opeid6": int("SCORECARD_OPEID6" in identifiers),
                "uei_match_status": uei_match_status,
            }
        )
    return universities


def _require_artifact(path: Path) -> Path:
    if not path.is_file():
        raise FeatureArtifactError(f"required feature artifact is missing or not a file: {path}")
    return path


def load_scorecard_institution_rows(output_dir: Path) -> dict[int, dict[str, Any]]:
    path = _require_artifact(output_dir / "scorecard_institution.parquet")
    rows = load_parquet_rows(path)
    return {int(row["university_id"]): row for row in rows}


def load_scorecard_field_rows(output_dir: Path) -> dict[int, list[dict[str, Any]]]:
    path = _require_artifact(output_dir / "scorecard_field_outcomes.parquet")
    rows = load_parquet_rows(path)
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[int(row["university_id"])].append(row)
    return grouped


def load_completion_rows(output_dir: Path) -> tuple[dict[int, list[dict[str, Any]]], list[int]]:
    rows = load_parquet_rows(_require_artifact(output_dir / "completions_by_year.parquet"))
    metadata = read_json(_require_artifact(output_dir / "completions_by_year_metadata.json"))
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[int(row["university_id"])].append(row)
    available_years = [int(year) for year in metadata.get("available_years", [])]
    if not available_years:
        raise FeatureArtifactError(
            "completions_by_year_metadata.json did not contain available_years"
        )
    lookback = available_years[-LOOKBACK_YEARS:]
    return grouped, lookback


def choose_research_counting_method(rows: list[dict[str, Any]]) -> str:
    available = {str(row["counting_method"]) for row in rows}
    for candidate in PREFERRED_COUNTING_METHODS:
        if candidate in available:
            return candidate
    if not available:
        raise RuntimeError("No OpenAlex metric rows were loaded.")
    return sorted(available)[0]


def load_research_rows(output_dir: Path) -> tuple[dict[int, list[dict[str, Any]]], Path, str]:
    try:
        run_dir = latest_openalex_run_dir(output_dir)
    except RuntimeError as exc:
        raise FeatureArtifactError(str(exc)) from exc
    rows = load_parquet_rows(_require_artifact(run_dir / "openalex_metrics.parquet"))
    chosen_method = choose_research_counting_method(rows)
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if str(row["counting_method"]) != chosen_method:
            continue
        grouped[int(row["university_id"])].append(row)
    return grouped, run_dir, chosen_method


def load_feature_artifacts(
    feature_config: FeatureConfig,
    conn: sqlite3.Connection,
    parquet_dir: Path,
) -> FeatureArtifacts:
    """Load every SQLite and Parquet artifact the feature builder needs.

    Accepts an open ``sqlite3.Connection`` rather than a path so callers can
    drive the function with an in-memory database in tests and so it shares
    the connection already opened by the loader framework at runtime.
    ``feature_config`` is accepted (rather than used for filtering here) so
    the signature documents the validation contract and leaves room for
    future config-driven loading without changing the caller again.
    """

    del feature_config  # validated upstream; reserved for future config-driven loads
    institution_rows = load_scorecard_institution_rows(parquet_dir)
    field_rows = load_scorecard_field_rows(parquet_dir)
    completion_rows, lookback_years = load_completion_rows(parquet_dir)
    research_rows, openalex_run_dir, research_counting_method = load_research_rows(parquet_dir)
    return FeatureArtifacts(
        universities=load_universities(conn),
        institution_rows=institution_rows,
        field_rows=field_rows,
        completion_rows=completion_rows,
        completion_lookback_years=lookback_years,
        research_rows=research_rows,
        openalex_run_dir=openalex_run_dir,
        research_counting_method=research_counting_method,
        grant_rows=load_grant_rows(conn),
        offer_rows=load_offer_rows(conn),
    )


def load_grant_rows(conn) -> dict[int, dict[str, dict[str, float]]]:
    rows = conn.execute(
        """
        SELECT
            u.university_id,
            g.source_code,
            u.year,
            COALESCE(u.grant_count, 0) AS grant_count,
            COALESCE(u.total_award_usd, 0) AS total_award_usd
        FROM university_grant_year_stats u
        JOIN grant_sources g
          ON g.grant_source_id = u.grant_source_id
        ORDER BY u.university_id, g.source_code, u.year
        """
    ).fetchall()
    grouped: dict[int, dict[str, dict[str, float]]] = defaultdict(dict)
    for row in rows:
        per_source = grouped[row["university_id"]].setdefault(
            row["source_code"], {"grant_count": 0.0, "total_award_usd": 0.0}
        )
        per_source["grant_count"] += float(row["grant_count"])
        per_source["total_award_usd"] += float(row["total_award_usd"])
    return grouped


def load_offer_rows(conn) -> dict[int, dict[str, tuple[int, int]]]:
    rows = conn.execute(
        """
        SELECT university_id, cip_code, first_year, last_year
        FROM university_cip_offers
        ORDER BY university_id, cip_code
        """
    ).fetchall()
    grouped: dict[int, dict[str, tuple[int, int]]] = defaultdict(dict)
    for row in rows:
        grouped[row["university_id"]][row["cip_code"]] = (
            int(row["first_year"]),
            int(row["last_year"]),
        )
    return grouped


def sum_completion_counts(
    rows: list[dict[str, Any]], cip_codes: set[str], lookback_years: set[int]
) -> tuple[int, dict[int, int]]:
    yearly: dict[int, int] = defaultdict(int)
    total = 0
    for row in rows:
        year = int(row["year"])
        if year not in lookback_years:
            continue
        if row["cip_code"] not in cip_codes:
            continue
        count = int(row["awards_count"] or 0)
        total += count
        yearly[year] += count
    return total, dict(yearly)


def total_bachelors_counts(
    rows: list[dict[str, Any]], lookback_years: set[int]
) -> tuple[int, dict[int, int]]:
    yearly_totals: dict[int, int] = {}
    fallback: dict[int, int] = defaultdict(int)
    for row in rows:
        year = int(row["year"])
        if year not in lookback_years:
            continue
        count = int(row["awards_count"] or 0)
        if row["cip_code"] == TOTAL_COMPLETIONS_CIP:
            yearly_totals[year] = yearly_totals.get(year, 0) + count
        else:
            fallback[year] += count
    merged = {
        year: yearly_totals.get(year, fallback.get(year, 0)) for year in sorted(lookback_years)
    }
    return sum(merged.values()), merged


def persistence_years(offers: dict[str, tuple[int, int]], cip_codes: set[str]) -> int:
    durations = [
        end_year - start_year + 1
        for cip_code, (start_year, end_year) in offers.items()
        if cip_code in cip_codes
    ]
    if not durations:
        return 0
    return max(durations)


def aggregate_research(
    rows: list[dict[str, Any]],
    subfield_weights: dict[int, float],
    research_years: list[int],
) -> dict[str, Any]:
    yearly_weighted = {year: 0.0 for year in research_years}
    weighted_impact_pairs: list[tuple[float | None, float]] = []
    fit_buckets = {"primary": 0.0, "secondary": 0.0, "adjacent": 0.0}
    subfield_fit = {
        subfield_id: (
            "primary" if weight >= 0.99 else "secondary" if weight >= 0.59 else "adjacent"
        )
        for subfield_id, weight in subfield_weights.items()
    }
    for row in rows:
        subfield_id = int(row["subfield_id"])
        if subfield_id not in subfield_weights:
            continue
        year = int(row["year"])
        weight = float(subfield_weights[subfield_id])
        publications = float(row["publications_count"] or 0.0)
        weighted_publications = publications * weight
        yearly_weighted[year] = yearly_weighted.get(year, 0.0) + weighted_publications
        fit_buckets[subfield_fit[subfield_id]] += weighted_publications
        weighted_impact_pairs.append((row["avg_citation_impact"], weighted_publications))

    active_years = sum(1 for value in yearly_weighted.values() if value > 0)
    trend_points = [(year, yearly_weighted[year]) for year in sorted(yearly_weighted)]
    return {
        "primary_weighted": fit_buckets["primary"],
        "secondary_weighted": fit_buckets["secondary"],
        "adjacent_weighted": fit_buckets["adjacent"],
        "total_weighted": sum(yearly_weighted.values()),
        "avg_citation_impact": weighted_mean(weighted_impact_pairs),
        "latest_year_weighted": yearly_weighted.get(max(research_years), 0.0),
        "trend_slope": linear_regression_slope(trend_points),
        "years_with_activity": active_years,
    }


def aggregate_outcomes(rows: list[dict[str, Any]], cip4_codes: set[str]) -> OutcomeAggregate:
    filtered = [row for row in rows if row["cip4_code"] in cip4_codes]
    if not filtered:
        return OutcomeAggregate(False, None, None, None, 0, 0)
    weights = []
    for row in filtered:
        raw_weight = row.get("completers_count")
        if raw_weight is None:
            raw_weight = (
                row.get("completers_count_year_2") or row.get("completers_count_year_1") or 0
            )
        weights.append(max(int(raw_weight or 0), 1))
    pairs_1yr = [
        (row.get("median_earnings_1yr"), weight)
        for row, weight in zip(filtered, weights, strict=True)
    ]
    pairs_5yr = [
        (row.get("median_earnings_5yr"), weight)
        for row, weight in zip(filtered, weights, strict=True)
    ]
    debt_pairs = [
        (
            row.get("debt_staff_grad_plus_eval_median")
            if row.get("debt_staff_grad_plus_eval_median") is not None
            else row.get("debt_staff_grad_plus_any_median"),
            weight,
        )
        for row, weight in zip(filtered, weights, strict=True)
    ]
    earnings_1yr = weighted_mean(pairs_1yr)
    earnings_5yr = weighted_mean(pairs_5yr)
    debt = weighted_mean(debt_pairs)
    available = any(value is not None for value in (earnings_1yr, earnings_5yr, debt))
    return OutcomeAggregate(
        available=available,
        earnings_1yr=earnings_1yr,
        earnings_5yr=earnings_5yr,
        debt=debt,
        completers=sum(int(row.get("completers_count") or 0) for row in filtered),
        row_count=len(filtered),
    )


def effective_outcomes(
    institution_row: dict[str, Any] | None,
    strict_outcomes: OutcomeAggregate,
    family_outcomes: OutcomeAggregate,
    allow_fallback: bool,
) -> dict[str, Any]:
    if strict_outcomes.available:
        return {
            "source": "field_direct",
            "using_institution_fallback": 0,
            "field_outcome_available": 1,
            "using_family_field_outcomes": 0,
            "earnings_1yr": strict_outcomes.earnings_1yr,
            "earnings_5yr": strict_outcomes.earnings_5yr,
            "debt": strict_outcomes.debt,
            "field_completers": strict_outcomes.completers,
        }
    if family_outcomes.available:
        return {
            "source": "field_family",
            "using_institution_fallback": 0,
            "field_outcome_available": 1,
            "using_family_field_outcomes": 1,
            "earnings_1yr": family_outcomes.earnings_1yr,
            "earnings_5yr": family_outcomes.earnings_5yr,
            "debt": family_outcomes.debt,
            "field_completers": family_outcomes.completers,
        }
    if institution_row and allow_fallback and institution_row.get("record_present"):
        earnings_5yr = (
            institution_row.get("median_earnings_5yr_after_completion")
            or institution_row.get("median_earnings_10yr_after_entry")
            or institution_row.get("median_earnings_8yr_after_entry")
            or institution_row.get("median_earnings_6yr_after_entry")
        )
        return {
            "source": "institution_fallback",
            "using_institution_fallback": 1,
            "field_outcome_available": 0,
            "using_family_field_outcomes": 0,
            "earnings_1yr": institution_row.get("median_earnings_1yr_after_completion"),
            "earnings_5yr": earnings_5yr,
            "debt": institution_row.get("median_debt_completers"),
            "field_completers": 0,
        }
    return {
        "source": "missing",
        "using_institution_fallback": 0,
        "field_outcome_available": 0,
        "using_family_field_outcomes": 0,
        "earnings_1yr": None,
        "earnings_5yr": None,
        "debt": None,
        "field_completers": 0,
    }


def selectivity_band(admission_rate: float | None) -> str:
    if admission_rate is None:
        return "unknown"
    if admission_rate < 0.10:
        return "ultra_selective"
    if admission_rate < 0.20:
        return "highly_selective"
    if admission_rate < 0.40:
        return "selective"
    if admission_rate < 0.70:
        return "inclusive"
    return "open_access"


def confidence_program(
    offers_any_target_cip: bool,
    family_completions: int,
    strict_completions: int,
    family_persistence: int,
    threshold: int,
) -> tuple[float, int]:
    thin_flag = int(family_completions < threshold * 2)
    score = (
        0.25 * int(offers_any_target_cip)
        + 0.35 * min(family_completions / max(threshold * 4, 20), 1.0)
        + 0.20 * min(strict_completions / 10.0, 1.0)
        + 0.20 * min(family_persistence / 10.0, 1.0)
    )
    return clamp(score), thin_flag


def confidence_research(
    family_weighted_publications: float,
    active_years: int,
    federal_grants_total: float,
    avg_citation_impact: float | None,
) -> tuple[float, int]:
    thin_flag = int(family_weighted_publications < 10.0 or active_years < 2)
    score = (
        0.45 * min(family_weighted_publications / 50.0, 1.0)
        + 0.25 * min(active_years / 5.0, 1.0)
        + 0.15 * min(federal_grants_total / 50_000_000.0, 1.0)
        + 0.15 * min((avg_citation_impact or 0.0) / 2.0, 1.0)
    )
    return clamp(score), thin_flag


def confidence_outcomes(source: str, completers: int) -> tuple[float, int]:
    if source == "field_direct":
        score = 0.75 + 0.25 * min(completers / 40.0, 1.0)
        return clamp(score), 0
    if source == "field_family":
        score = 0.60 + 0.20 * min(completers / 50.0, 1.0)
        return clamp(score), 0
    if source == "institution_fallback":
        return 0.60, 1
    return 0.0, 1


def confidence_mapping(
    offered_count: int,
    mapped_count: int,
    subfield_weights: dict[int, float],
) -> tuple[float, int]:
    if offered_count == 0:
        return 0.0, 1
    offered_share = mapped_count / offered_count
    mean_weight = (
        sum(subfield_weights.values()) / len(subfield_weights) if subfield_weights else 0.0
    )
    score = 0.65 * offered_share + 0.35 * mean_weight
    return clamp(score), int(offered_share < 0.5 or not subfield_weights)


def confidence_identity(university: dict[str, Any]) -> float:
    score = (
        0.40 * university["has_openalex_id"]
        + 0.30 * university["has_scorecard_unitid"]
        + 0.30 * university["has_scorecard_opeid6"]
    )
    # Apply a small penalty for UEIs that were resolved automatically rather than
    # verified against institution-published grant pages.
    uei_status = university.get("uei_match_status")
    if uei_status == "automated":
        score -= 0.05
    return clamp(score)


def merge_cip_subfields(
    artifacts: FeatureArtifacts,
    config: FeatureConfig,
) -> MergedFeatureData:
    """Merge CIP-keyed completion counts with subfield-keyed research metrics.

    For each (university, cluster) pair this joins:
      * completion rows (keyed by CIP) restricted to the cluster's strict and
        family CIP sets,
      * research rows (keyed by OpenAlex subfield) projected through the
        cluster's CIP→subfield weight mapping,
      * program persistence and offered-CIP context.
    """

    research_years = list(range(config.research_start, config.research_end + 1))
    lookback_years = artifacts.completion_lookback_years
    lookback_set = set(lookback_years)
    cluster_cip_sets = [
        (
            set(cluster.strict_cips),
            set(cluster.family_cips),
            set(cluster.strict_mapped_cips),
            set(cluster.family_mapped_cips),
        )
        for cluster in config.clusters
    ]
    merged_rows: list[MergedClusterFeatures] = []
    for university in artifacts.universities:
        university_id = int(university["university_id"])
        offers = artifacts.offer_rows.get(university_id, {})
        completion_rows = artifacts.completion_rows.get(university_id, [])
        research_rows = artifacts.research_rows.get(university_id, [])
        total_bachelors, total_bachelors_by_year = total_bachelors_counts(
            completion_rows, lookback_set
        )
        for cluster, (
            strict_cip_set,
            family_cip_set,
            strict_mapped_set,
            family_mapped_set,
        ) in zip(config.clusters, cluster_cip_sets, strict=True):
            strict_offers = tuple(sorted(cip for cip in offers if cip in strict_cip_set))
            family_offers = tuple(sorted(cip for cip in offers if cip in family_cip_set))
            strict_unmapped = tuple(sorted(set(strict_offers) - strict_mapped_set))
            family_unmapped = tuple(sorted(set(family_offers) - family_mapped_set))
            strict_completions, strict_completions_by_year = sum_completion_counts(
                completion_rows, strict_cip_set, lookback_set
            )
            family_completions, family_completions_by_year = sum_completion_counts(
                completion_rows, family_cip_set, lookback_set
            )
            strict_research = aggregate_research(
                research_rows, cluster.strict_subfield_weights, research_years
            )
            family_research = aggregate_research(
                research_rows, cluster.family_subfield_weights, research_years
            )
            merged_rows.append(
                MergedClusterFeatures(
                    university_id=university_id,
                    cluster_slug=cluster.slug,
                    strict_offers=strict_offers,
                    family_offers=family_offers,
                    strict_unmapped_offers=strict_unmapped,
                    family_unmapped_offers=family_unmapped,
                    strict_completions=strict_completions,
                    strict_completions_by_year=strict_completions_by_year,
                    family_completions=family_completions,
                    family_completions_by_year=family_completions_by_year,
                    total_bachelors=total_bachelors,
                    total_bachelors_by_year=total_bachelors_by_year,
                    strict_persistence_years=persistence_years(offers, strict_cip_set),
                    family_persistence_years=persistence_years(offers, family_cip_set),
                    strict_research=strict_research,
                    family_research=family_research,
                )
            )
    return MergedFeatureData(
        rows=tuple(merged_rows),
        research_years=tuple(research_years),
        lookback_years=tuple(lookback_years),
    )


def compute_cluster_scores(
    data: MergedFeatureData,
    config: FeatureConfig,
) -> ScoredFeatures:
    """Compute per-(university, cluster) scores from merged feature data.

    Pure function: takes the merged data plus the validated config and
    returns the program/mapping confidence components, the completion
    shares, and the eligibility flags. The research/outcomes/identity
    confidence components are computed by the caller because they need
    inputs (grant totals, Scorecard outcomes, identifier flags) that are
    not part of MergedFeatureData.
    """

    clusters_by_slug = {cluster.slug: cluster for cluster in config.clusters}
    threshold = config.min_recent_bachelors_in_target_family

    scored: list[ClusterScore] = []
    for row in data.rows:
        cluster = clusters_by_slug[row.cluster_slug]

        direct_share = row.strict_completions / row.total_bachelors if row.total_bachelors else None
        family_share = row.family_completions / row.total_bachelors if row.total_bachelors else None

        program_confidence, thin_completion_flag = confidence_program(
            bool(row.family_offers),
            row.family_completions,
            row.strict_completions,
            row.family_persistence_years,
            threshold,
        )
        mapping_confidence, sparse_mapping_flag = confidence_mapping(
            len(row.family_offers),
            len(row.family_offers) - len(row.family_unmapped_offers),
            cluster.family_subfield_weights,
        )

        offers_any_target_cip = int(bool(row.family_offers))
        min_threshold_passed = int(row.family_completions >= threshold)
        eligible = int(offers_any_target_cip and min_threshold_passed)

        exclusion_reasons: list[str] = []
        if not offers_any_target_cip:
            exclusion_reasons.append("no_target_offer")
        if offers_any_target_cip and not min_threshold_passed:
            exclusion_reasons.append("below_min_recent_bachelors_threshold")

        scored.append(
            ClusterScore(
                university_id=row.university_id,
                cluster_slug=row.cluster_slug,
                target_completions_share_direct=direct_share,
                target_completions_share_family=family_share,
                confidence_program=program_confidence,
                confidence_mapping=mapping_confidence,
                thin_completion_flag=thin_completion_flag,
                sparse_mapping_flag=sparse_mapping_flag,
                offers_any_target_cip=offers_any_target_cip,
                min_recent_bachelors_threshold_passed=min_threshold_passed,
                eligible_for_ranking=eligible,
                exclusion_reasons=tuple(exclusion_reasons),
            )
        )

    return ScoredFeatures(rows=tuple(scored))


def build_cluster_feature_rows(
    artifacts: FeatureArtifacts,
    config: FeatureConfig,
    *,
    output_dir: Path,
) -> list[dict[str, Any]]:
    """Build the whole feature store: one row per (university, field cluster).

    This owns the entire row. It joins CIPs to OpenAlex subfields, scores
    eligibility, aggregates Scorecard outcomes, resolves the five confidence
    components, derives the grant and cost ratios, and formats the flat row
    declared by ``shared.feature_store.COLUMNS``.

    Those steps used to be split across three functions, with the second half
    living inside the parquet writer — so the tested pure functions covered the
    easy half and the rest was only reachable through a file write.
    ``write_feature_store`` is now just the writer.

    ``output_dir`` is needed only to render ``source_openalex_run_dir`` relative
    to its parent.
    """
    merged = merge_cip_subfields(artifacts, config)
    scores = compute_cluster_scores(merged, config)

    features_by_key = {
        (m.university_id, m.cluster_slug): (m, s)
        for m, s in zip(merged.rows, scores.rows, strict=True)
    }
    universities = artifacts.universities
    clusters = list(config.clusters)
    institution_rows = artifacts.institution_rows
    field_rows = artifacts.field_rows
    openalex_run_dir = artifacts.openalex_run_dir
    research_counting_method = artifacts.research_counting_method
    grant_rows = artifacts.grant_rows
    lookback_years = merged.lookback_years
    allow_fallback = config.allow_institution_level_outcome_fallback

    rows: list[dict[str, Any]] = []
    for university in universities:
        university_id = int(university["university_id"])
        institution = institution_rows.get(university_id)
        university_field_rows = field_rows.get(university_id, [])
        university_grants = grant_rows.get(university_id, {})

        for cluster in clusters:
            merged_features, score = features_by_key[(university_id, cluster.slug)]
            strict_offers = merged_features.strict_offers
            family_offers = merged_features.family_offers
            strict_unmapped = merged_features.strict_unmapped_offers
            family_unmapped = merged_features.family_unmapped_offers
            strict_completions = merged_features.strict_completions
            strict_completions_by_year = merged_features.strict_completions_by_year
            family_completions = merged_features.family_completions
            family_completions_by_year = merged_features.family_completions_by_year
            total_bachelors = merged_features.total_bachelors
            total_bachelors_by_year = merged_features.total_bachelors_by_year
            strict_persistence = merged_features.strict_persistence_years
            family_persistence = merged_features.family_persistence_years
            strict_research = merged_features.strict_research
            family_research = merged_features.family_research

            direct_share = score.target_completions_share_direct
            family_share = score.target_completions_share_family

            strict_outcomes = aggregate_outcomes(university_field_rows, set(cluster.strict_cip4s))
            family_outcomes = aggregate_outcomes(university_field_rows, set(cluster.family_cip4s))
            effective = effective_outcomes(
                institution, strict_outcomes, family_outcomes, allow_fallback
            )

            usaspending_total = university_grants.get("USASPENDING_ALL_FEDERAL", {}).get(
                "total_award_usd", 0.0
            )
            nih_total = university_grants.get("NIH", {}).get("total_award_usd", 0.0)
            nsf_total = university_grants.get("NSF", {}).get("total_award_usd", 0.0)
            federal_grants_total = usaspending_total  # USAspending is the canonical all-federal total; NIH and NSF are sub-components already included
            federal_grants_count = sum(
                source.get("grant_count", 0.0) for source in university_grants.values()
            )
            grant_sources_present_count = sum(
                1 for source in university_grants.values() if source.get("total_award_usd", 0.0) > 0
            )
            grant_intensity = (
                federal_grants_total / family_research["total_weighted"]
                if family_research["total_weighted"] > 0
                else None
            )

            admission_rate = institution.get("admission_rate") if institution else None
            pell_share = institution.get("pell_grant_rate") if institution else None
            avg_annual_cost = institution.get("avg_net_price_overall") if institution else None
            debt_to_earnings = (
                effective["debt"] / effective["earnings_5yr"]
                if effective["debt"] not in (None, 0) and effective["earnings_5yr"] not in (None, 0)
                else None
            )

            program_confidence = score.confidence_program
            thin_completion_flag = score.thin_completion_flag
            research_confidence, thin_research_flag = confidence_research(
                family_research["total_weighted"],
                family_research["years_with_activity"],
                federal_grants_total,
                family_research["avg_citation_impact"],
            )
            outcomes_confidence, missing_field_outcomes_flag = confidence_outcomes(
                effective["source"], effective["field_completers"]
            )
            mapping_confidence = score.confidence_mapping
            sparse_mapping_flag = score.sparse_mapping_flag
            identity_confidence = confidence_identity(university)

            offers_any_target_cip = score.offers_any_target_cip
            min_recent_bachelors_threshold_passed = score.min_recent_bachelors_threshold_passed
            eligible_for_ranking = score.eligible_for_ranking
            exclusion_reasons = score.exclusion_reasons

            missing_feature_count = sum(
                value is None
                for value in (
                    family_share,
                    family_research["avg_citation_impact"],
                    effective["earnings_5yr"],
                    avg_annual_cost,
                    admission_rate,
                    pell_share,
                    debt_to_earnings,
                )
            )
            missing_critical_feature_count = sum(
                value is None
                for value in (
                    effective["earnings_5yr"],
                    avg_annual_cost,
                    admission_rate,
                )
            )

            rows.append(
                {
                    "university_id": university_id,
                    "canonical_name": university["canonical_name"],
                    "field_cluster_slug": cluster.slug,
                    "field_cluster_label": cluster.label,
                    "scoring_profile": cluster.scoring_profile,
                    "state_code": university["state_code"],
                    "city": university["city"],
                    "control": university["control"],
                    "country_code": university["country_code"],
                    "offers_any_target_cip": offers_any_target_cip,
                    "offered_direct_cip_count": len(strict_offers),
                    "offered_family_cip_count": len(family_offers),
                    "strict_target_cip_count": len(cluster.strict_cips),
                    "family_target_cip_count": len(cluster.family_cips),
                    "strict_target_subfield_count": len(cluster.strict_subfield_weights),
                    "family_target_subfield_count": len(cluster.family_subfield_weights),
                    "strict_unmapped_offered_cip_count": len(strict_unmapped),
                    "family_unmapped_offered_cip_count": len(family_unmapped),
                    "strict_unmapped_offered_cips": ",".join(strict_unmapped),
                    "family_unmapped_offered_cips": ",".join(family_unmapped),
                    "recent_bachelors_completions_direct": strict_completions,
                    "recent_bachelors_completions_family": family_completions,
                    "recent_total_bachelors": total_bachelors,
                    "recent_bachelors_completions_direct_by_year": ",".join(
                        f"{year}:{strict_completions_by_year.get(year, 0)}"
                        for year in lookback_years
                    ),
                    "recent_bachelors_completions_family_by_year": ",".join(
                        f"{year}:{family_completions_by_year.get(year, 0)}"
                        for year in lookback_years
                    ),
                    "recent_total_bachelors_by_year": ",".join(
                        f"{year}:{total_bachelors_by_year.get(year, 0)}" for year in lookback_years
                    ),
                    "target_completions_share_direct": direct_share,
                    "target_completions_share_family": family_share,
                    "program_persistence_years_direct": strict_persistence,
                    "program_persistence_years_family": family_persistence,
                    "min_recent_bachelors_threshold_passed": min_recent_bachelors_threshold_passed,
                    "completion_lookback_start": min(lookback_years),
                    "completion_lookback_end": max(lookback_years),
                    "research_publications_direct_weighted_5y": round(
                        strict_research["total_weighted"], 6
                    ),
                    "research_publications_family_weighted_5y": round(
                        family_research["total_weighted"], 6
                    ),
                    "research_publications_primary_weighted_5y": round(
                        family_research["primary_weighted"], 6
                    ),
                    "research_publications_secondary_weighted_5y": round(
                        family_research["secondary_weighted"], 6
                    ),
                    "research_publications_adjacent_weighted_5y": round(
                        family_research["adjacent_weighted"], 6
                    ),
                    "research_avg_citation_impact_direct": strict_research["avg_citation_impact"],
                    "research_avg_citation_impact_family": family_research["avg_citation_impact"],
                    "research_latest_year_publications_direct_weighted": round(
                        strict_research["latest_year_weighted"], 6
                    ),
                    "research_latest_year_publications_family_weighted": round(
                        family_research["latest_year_weighted"], 6
                    ),
                    "research_trend_slope_direct": strict_research["trend_slope"],
                    "research_trend_slope_family": family_research["trend_slope"],
                    "research_years_with_activity_direct": strict_research["years_with_activity"],
                    "research_years_with_activity_family": family_research["years_with_activity"],
                    "research_counting_method": research_counting_method,
                    "usaspending_total_5y": usaspending_total,
                    "nih_total_5y": nih_total,
                    "nsf_total_5y": nsf_total,
                    "federal_grants_total_5y": federal_grants_total,
                    "federal_grants_count_5y": federal_grants_count,
                    "grant_sources_present_count": grant_sources_present_count,
                    "grant_intensity_per_publication": grant_intensity,
                    "scorecard_direct_earnings_1yr": strict_outcomes.earnings_1yr,
                    "scorecard_direct_earnings_5yr": strict_outcomes.earnings_5yr,
                    "scorecard_direct_median_debt": strict_outcomes.debt,
                    "scorecard_direct_completers": strict_outcomes.completers,
                    "scorecard_family_earnings_1yr": family_outcomes.earnings_1yr,
                    "scorecard_family_earnings_5yr": family_outcomes.earnings_5yr,
                    "scorecard_family_median_debt": family_outcomes.debt,
                    "scorecard_family_completers": family_outcomes.completers,
                    "effective_outcomes_source": effective["source"],
                    "field_outcome_available": effective["field_outcome_available"],
                    "using_family_field_outcomes": effective["using_family_field_outcomes"],
                    "using_institution_fallback": effective["using_institution_fallback"],
                    "student_outcomes_earnings_1yr": effective["earnings_1yr"],
                    "student_outcomes_earnings_5yr": effective["earnings_5yr"],
                    "student_outcomes_debt": effective["debt"],
                    "scorecard_institution_completion_rate": institution.get("completion_rate")
                    if institution
                    else None,
                    "scorecard_institution_retention_rate": institution.get("retention_rate")
                    if institution
                    else None,
                    "avg_annual_cost": avg_annual_cost,
                    "net_price_0_30000": institution.get("net_price_0_30000")
                    if institution
                    else None,
                    "net_price_30001_48000": institution.get("net_price_30001_48000")
                    if institution
                    else None,
                    "net_price_48001_75000": institution.get("net_price_48001_75000")
                    if institution
                    else None,
                    "net_price_75001_110000": institution.get("net_price_75001_110000")
                    if institution
                    else None,
                    "net_price_110001_plus": institution.get("net_price_110001_plus")
                    if institution
                    else None,
                    "debt_to_earnings_proxy": debt_to_earnings,
                    "admission_rate": admission_rate,
                    "sat_average_overall": institution.get("sat_average_overall")
                    if institution
                    else None,
                    "sat_50_math": institution.get("sat_50_math") if institution else None,
                    "sat_50_reading": institution.get("sat_50_reading") if institution else None,
                    "act_50_cumulative": institution.get("act_50_cumulative")
                    if institution
                    else None,
                    "pell_share": pell_share,
                    "selectivity_band": selectivity_band(admission_rate),
                    "has_undergrad_research_flag": int(
                        bool(family_offers and strict_research["total_weighted"] > 0)
                    ),
                    "has_internship_flag": 0,
                    "has_study_abroad_flag": 0,
                    "confidence_program": round(program_confidence, 6),
                    "confidence_research": round(research_confidence, 6),
                    "confidence_outcomes": round(outcomes_confidence, 6),
                    "confidence_mapping": round(mapping_confidence, 6),
                    "confidence_identity": round(identity_confidence, 6),
                    "missing_feature_count": missing_feature_count,
                    "missing_critical_feature_count": missing_critical_feature_count,
                    "thin_completion_flag": thin_completion_flag,
                    "thin_research_flag": thin_research_flag,
                    "missing_field_outcomes_flag": missing_field_outcomes_flag,
                    "sparse_mapping_flag": sparse_mapping_flag,
                    "eligible_for_ranking": eligible_for_ranking,
                    "exclusion_reason": ",".join(exclusion_reasons),
                    "source_openalex_run_dir": str(openalex_run_dir.relative_to(output_dir.parent)),
                }
            )

    validate_emitted_rows(rows)
    return rows


def write_feature_store(
    rows: list[dict[str, Any]],
    artifacts: FeatureArtifacts,
    config: FeatureConfig,
    output_dir: Path,
    *,
    execute: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Write the feature store and describe the run.

    A thin adapter over ``build_cluster_feature_rows``: it decides where the
    parquet goes and what the manifest says, and computes nothing about a
    university.
    """
    feature_store_path = output_dir / "feature_store.parquet"
    if execute:
        write_parquet(feature_store_path, rows)

    summary = {
        "feature_store_rows": len(rows),
        "feature_store_path": str(feature_store_path),
        "cluster_count": len(config.clusters),
        "feature_store_columns": len(COLUMNS),
        "university_count": len(artifacts.universities),
        "completion_lookback_years": list(artifacts.completion_lookback_years),
        "research_counting_method": artifacts.research_counting_method,
        "openalex_run_dir": str(artifacts.openalex_run_dir),
    }
    return rows, summary


class BuildFieldClusterFeaturesLoader(ManualDataLoader):
    source_code = "BUILD_FIELD_CLUSTER_FEATURES"
    description = (
        "Build the Phase 3 field-cluster feature store from SQLite plus Phase 2 artifacts."
    )
    execute_help = "write outputs/current/feature_store.parquet (default is dry-run)"

    def load(
        self,
        *,
        conn,
        config: dict[str, Any],
        args,
        snapshot: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        conn = self._require_conn(conn)
        output_dir = resolve_output_dir(config)
        feature_config = validate_feature_config(config, conn)
        artifacts = load_feature_artifacts(feature_config, conn, output_dir)
        rows = build_cluster_feature_rows(artifacts, feature_config, output_dir=output_dir)
        rows, summary = write_feature_store(
            rows, artifacts, feature_config, output_dir, execute=args.execute
        )
        eligible_rows = sum(int(row["eligible_for_ranking"]) for row in rows)
        return rows, {
            "executed": args.execute,
            "eligible_rows": eligible_rows,
            **summary,
        }


if __name__ == "__main__":
    raise SystemExit(BuildFieldClusterFeaturesLoader().run())
