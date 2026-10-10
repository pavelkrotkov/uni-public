from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from build_field_cluster_features import (
    BuildFieldClusterFeaturesLoader,
    FeatureArtifactError,
    FeatureArtifacts,
    FeatureConfigError,
    build_cluster_feature_rows,
    compute_cluster_scores,
    load_feature_artifacts,
    merge_cip_subfields,
    validate_feature_config,
    write_feature_store,
)


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE cips (
            cip_code TEXT PRIMARY KEY,
            cip_family_code TEXT NOT NULL
        );
        CREATE TABLE cip_trains_subfield (
            cip_code TEXT NOT NULL,
            fit_level TEXT NOT NULL,
            subfield_id INTEGER NOT NULL
        );
        INSERT INTO cips (cip_code, cip_family_code)
        VALUES
            ('26.0202', '26'),
            ('26.0203', '26'),
            ('27.0501', '27');
        INSERT INTO cip_trains_subfield (cip_code, fit_level, subfield_id)
        VALUES
            ('26.0202', 'primary', 1103),
            ('26.0203', 'secondary', 1110),
            ('27.0501', 'adjacent', 1804);
        """
    )
    return conn


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    db = _make_conn()
    try:
        yield db
    finally:
        db.close()


def _config() -> dict[str, Any]:
    return {
        "years": {"research_start": 2020, "research_end": 2024},
        "institution_filters": {"min_recent_bachelors_in_target_family": 5},
        "feature_flags": {"allow_institution_level_outcome_fallback": True},
        "scoring_profiles": {"balanced": {"weights": {"program_availability": 1.0}}},
        "field_clusters": [
            {
                "slug": "molbio",
                "label": "Molecular Biology",
                "cip_rules": {
                    "include_6digit": ["26.0202"],
                    "include_2digit_families": ["26"],
                    "exclude_6digit": [],
                },
                "fit_weights": {"primary": 1.0, "secondary": 0.6, "adjacent": 0.25},
                "scoring_profile": "balanced",
            }
        ],
    }


def test_validate_feature_config_accepts_valid_config(conn: sqlite3.Connection) -> None:
    feature_config = validate_feature_config(_config(), conn)
    assert feature_config.research_start == 2020
    assert feature_config.research_end == 2024
    assert feature_config.min_recent_bachelors_in_target_family == 5
    assert feature_config.allow_institution_level_outcome_fallback is True
    assert len(feature_config.clusters) == 1
    cluster = feature_config.clusters[0]
    assert cluster.slug == "molbio"
    assert cluster.strict_cips == ("26.0202",)
    assert cluster.family_cips == ("26.0202", "26.0203")
    assert cluster.strict_subfield_weights == {1103: 1.0}


def test_validate_feature_config_rejects_missing_cluster_key(conn: sqlite3.Connection) -> None:
    config = _config()
    del config["field_clusters"][0]["label"]
    with pytest.raises(FeatureConfigError, match="missing required keys: label"):
        validate_feature_config(config, conn)


def test_validate_feature_config_rejects_unknown_cip_code(conn: sqlite3.Connection) -> None:
    config = _config()
    config["field_clusters"][0]["cip_rules"]["include_6digit"] = ["99.9999"]
    with pytest.raises(FeatureConfigError, match=r"unknown CIP codes: 99\.9999"):
        validate_feature_config(config, conn)


def test_validate_feature_config_rejects_invalid_weight(conn: sqlite3.Connection) -> None:
    config = _config()
    config["field_clusters"][0]["fit_weights"]["primary"] = -1
    with pytest.raises(FeatureConfigError, match="expected non-negative weight"):
        validate_feature_config(config, conn)


def test_validate_feature_config_rejects_duplicate_slug(conn: sqlite3.Connection) -> None:
    config = _config()
    config["field_clusters"].append(dict(config["field_clusters"][0]))
    with pytest.raises(FeatureConfigError, match="duplicate slug: molbio"):
        validate_feature_config(config, conn)


def test_validate_feature_config_rejects_unknown_fit_level(conn: sqlite3.Connection) -> None:
    config = _config()
    config["field_clusters"][0]["fit_weights"] = {"bogus": 1.0}
    with pytest.raises(FeatureConfigError, match="unknown fit levels: bogus"):
        validate_feature_config(config, conn)


def test_validate_feature_config_rejects_unknown_cip_family(conn: sqlite3.Connection) -> None:
    config = _config()
    config["field_clusters"][0]["cip_rules"]["include_2digit_families"] = ["99"]
    with pytest.raises(FeatureConfigError, match="unknown CIP family codes: 99"):
        validate_feature_config(config, conn)


def test_validate_feature_config_rejects_inverted_research_years(conn: sqlite3.Connection) -> None:
    config = _config()
    config["years"] = {"research_start": 2024, "research_end": 2020}
    with pytest.raises(FeatureConfigError, match="research_start must be <= research_end"):
        validate_feature_config(config, conn)


def test_validate_feature_config_rejects_unknown_scoring_profile(conn: sqlite3.Connection) -> None:
    config = _config()
    config["field_clusters"][0]["scoring_profile"] = "research_heavy"
    with pytest.raises(FeatureConfigError, match="unknown scoring profile: research_heavy"):
        validate_feature_config(config, conn)


def test_validate_feature_config_rejects_empty_scoring_profiles(conn: sqlite3.Connection) -> None:
    config = _config()
    config["scoring_profiles"] = {}
    with pytest.raises(FeatureConfigError, match="must contain at least one profile"):
        validate_feature_config(config, conn)


def test_validate_feature_config_rejects_cluster_resolving_to_zero_cips(
    conn: sqlite3.Connection,
) -> None:
    config = _config()
    config["field_clusters"][0]["cip_rules"] = {
        "include_6digit": [],
        "include_2digit_families": [],
        "exclude_6digit": [],
    }
    with pytest.raises(FeatureConfigError, match="resolved to zero CIP codes"):
        validate_feature_config(config, conn)


def test_validate_feature_config_rejects_all_zero_fit_weights(conn: sqlite3.Connection) -> None:
    config = _config()
    config["field_clusters"][0]["fit_weights"] = {"primary": 0, "secondary": 0, "adjacent": 0}
    with pytest.raises(FeatureConfigError, match="at least one weight must be greater than zero"):
        validate_feature_config(config, conn)


def _make_artifacts_conn() -> sqlite3.Connection:
    conn = _make_conn()
    conn.executescript(
        """
        CREATE TABLE universities (
            university_id INTEGER PRIMARY KEY,
            canonical_name TEXT NOT NULL,
            state_code TEXT,
            city TEXT,
            control TEXT,
            country_code TEXT
        );
        CREATE TABLE university_identifiers (
            university_id INTEGER NOT NULL,
            system_code TEXT NOT NULL,
            external_id TEXT NOT NULL,
            match_status TEXT
        );
        CREATE TABLE grant_sources (
            grant_source_id INTEGER PRIMARY KEY,
            source_code TEXT NOT NULL UNIQUE
        );
        CREATE TABLE university_grant_year_stats (
            university_id INTEGER NOT NULL,
            grant_source_id INTEGER NOT NULL,
            year INTEGER NOT NULL,
            grant_count INTEGER,
            total_award_usd REAL
        );
        CREATE TABLE university_cip_offers (
            university_id INTEGER NOT NULL,
            cip_code TEXT NOT NULL,
            first_year INTEGER NOT NULL,
            last_year INTEGER NOT NULL
        );
        INSERT INTO universities VALUES
            (1, 'Test U', 'CA', 'Pasadena', 'private', 'US');
        INSERT INTO university_identifiers VALUES
            (1, 'OPENALEX', 'I123', 'verified'),
            (1, 'SCORECARD_UNITID', '999', 'verified');
        INSERT INTO grant_sources VALUES (1, 'NIH'), (2, 'USASPENDING_ALL_FEDERAL');
        INSERT INTO university_grant_year_stats VALUES
            (1, 1, 2023, 4, 1000000.0),
            (1, 2, 2023, 10, 5000000.0);
        INSERT INTO university_cip_offers VALUES
            (1, '26.0202', 2018, 2024);
        """
    )
    return conn


def _write_parquet(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path)


def _write_feature_parquet_fixtures(parquet_dir: Path) -> None:
    _write_parquet(
        parquet_dir / "scorecard_institution.parquet",
        [
            {
                "university_id": 1,
                "record_present": 1,
                "admission_rate": 0.1,
                "pell_grant_rate": 0.2,
                "avg_net_price_overall": 30000.0,
            }
        ],
    )
    _write_parquet(
        parquet_dir / "scorecard_field_outcomes.parquet",
        [
            {
                "university_id": 1,
                "cip4_code": "2602",
                "completers_count": 25,
                "median_earnings_1yr": 60000.0,
                "median_earnings_5yr": 90000.0,
                "debt_staff_grad_plus_eval_median": 18000.0,
            }
        ],
    )
    _write_parquet(
        parquet_dir / "completions_by_year.parquet",
        [
            {"university_id": 1, "cip_code": "26.0202", "year": 2023, "awards_count": 12},
            {"university_id": 1, "cip_code": "00.0099", "year": 2023, "awards_count": 200},
        ],
    )
    (parquet_dir / "completions_by_year_metadata.json").write_text(
        json.dumps({"available_years": [2019, 2020, 2021, 2022, 2023]})
    )
    run_dir = parquet_dir / "openalex-2024-01-01"
    _write_parquet(
        run_dir / "openalex_metrics.parquet",
        [
            {
                "university_id": 1,
                "subfield_id": 1103,
                "year": 2023,
                "counting_method": "primary_subfield",
                "publications_count": 5.0,
                "avg_citation_impact": 1.5,
            }
        ],
    )


def test_load_feature_artifacts_happy_path(tmp_path: Path) -> None:
    conn = _make_artifacts_conn()
    try:
        feature_config = validate_feature_config(_config(), conn)
        _write_feature_parquet_fixtures(tmp_path)
        artifacts = load_feature_artifacts(feature_config, conn, tmp_path)
        assert len(artifacts.universities) == 1
        assert artifacts.universities[0]["university_id"] == 1
        assert artifacts.universities[0]["has_openalex_id"] == 1
        assert artifacts.universities[0]["uei_match_status"] is None
        assert 1 in artifacts.institution_rows
        assert artifacts.field_rows[1][0]["cip4_code"] == "2602"
        assert artifacts.completion_lookback_years == [2019, 2020, 2021, 2022, 2023]
        assert artifacts.completion_rows[1][0]["cip_code"] == "26.0202"
        assert artifacts.research_counting_method == "primary_subfield"
        assert artifacts.research_rows[1][0]["subfield_id"] == 1103
        assert artifacts.openalex_run_dir == tmp_path / "openalex-2024-01-01"
        assert artifacts.grant_rows[1]["NIH"]["grant_count"] == 4.0
        assert artifacts.offer_rows[1]["26.0202"] == (2018, 2024)
    finally:
        conn.close()


def test_load_feature_artifacts_missing_parquet_raises(tmp_path: Path) -> None:
    conn = _make_artifacts_conn()
    try:
        feature_config = validate_feature_config(_config(), conn)
        _write_feature_parquet_fixtures(tmp_path)
        (tmp_path / "completions_by_year.parquet").unlink()
        with pytest.raises(FeatureArtifactError, match=r"completions_by_year\.parquet"):
            load_feature_artifacts(feature_config, conn, tmp_path)
    finally:
        conn.close()


def test_load_feature_artifacts_rejects_directory_in_artifact_slot(tmp_path: Path) -> None:
    conn = _make_artifacts_conn()
    try:
        feature_config = validate_feature_config(_config(), conn)
        _write_feature_parquet_fixtures(tmp_path)
        target = tmp_path / "scorecard_institution.parquet"
        target.unlink()
        target.mkdir()
        with pytest.raises(FeatureArtifactError, match=r"not a file"):
            load_feature_artifacts(feature_config, conn, tmp_path)
    finally:
        conn.close()


def _artifacts(
    *,
    universities: list[dict[str, Any]] | None = None,
    completion_rows: dict[int, list[dict[str, Any]]] | None = None,
    research_rows: dict[int, list[dict[str, Any]]] | None = None,
    offer_rows: dict[int, dict[str, tuple[int, int]]] | None = None,
    lookback_years: list[int] | None = None,
) -> FeatureArtifacts:
    return FeatureArtifacts(
        universities=universities if universities is not None else [{"university_id": 1}],
        institution_rows={},
        field_rows={},
        completion_rows=completion_rows if completion_rows is not None else {},
        completion_lookback_years=lookback_years
        if lookback_years is not None
        else [2019, 2020, 2021, 2022, 2023],
        research_rows=research_rows if research_rows is not None else {},
        openalex_run_dir=Path("/unused"),
        research_counting_method="primary_subfield",
        grant_rows={},
        offer_rows=offer_rows if offer_rows is not None else {},
    )


def test_merge_cip_subfields_standard_merge(conn: sqlite3.Connection) -> None:
    feature_config = validate_feature_config(_config(), conn)
    artifacts = _artifacts(
        completion_rows={
            1: [
                {"cip_code": "26.0202", "year": 2023, "awards_count": 12},
                {"cip_code": "26.0203", "year": 2023, "awards_count": 4},
                {"cip_code": "00.0099", "year": 2023, "awards_count": 200},
            ]
        },
        research_rows={
            1: [
                {
                    "subfield_id": 1103,
                    "year": 2023,
                    "publications_count": 5.0,
                    "avg_citation_impact": 1.5,
                }
            ]
        },
        offer_rows={1: {"26.0202": (2018, 2024), "26.0203": (2020, 2024)}},
    )
    merged = merge_cip_subfields(artifacts, feature_config)
    assert merged.research_years == tuple(range(2020, 2025))
    assert merged.lookback_years == (2019, 2020, 2021, 2022, 2023)
    assert len(merged.rows) == 1
    row = merged.rows[0]
    assert row.university_id == 1
    assert row.cluster_slug == "molbio"
    assert row.strict_offers == ("26.0202",)
    assert row.family_offers == ("26.0202", "26.0203")
    assert row.strict_unmapped_offers == ()
    assert row.family_unmapped_offers == ()
    assert row.strict_completions == 12
    assert row.family_completions == 16
    assert row.total_bachelors == 200
    assert row.strict_persistence_years == 7
    assert row.family_persistence_years == 7
    assert row.strict_research["total_weighted"] == pytest.approx(5.0)
    assert row.strict_research["primary_weighted"] == pytest.approx(5.0)
    assert row.family_research["total_weighted"] == pytest.approx(5.0)


def test_merge_cip_subfields_cip_with_no_matching_subfield(conn: sqlite3.Connection) -> None:
    config = _config()
    config["field_clusters"][0]["cip_rules"] = {
        "include_6digit": ["27.0501"],
        "include_2digit_families": [],
        "exclude_6digit": [],
    }
    config["field_clusters"][0]["fit_weights"] = {"primary": 1.0}
    feature_config = validate_feature_config(config, conn)
    cluster = feature_config.clusters[0]
    assert cluster.strict_subfield_weights == {}
    artifacts = _artifacts(
        completion_rows={
            1: [{"cip_code": "27.0501", "year": 2023, "awards_count": 8}],
        },
        research_rows={
            1: [
                {
                    "subfield_id": 1103,
                    "year": 2023,
                    "publications_count": 9.0,
                    "avg_citation_impact": 2.0,
                }
            ]
        },
        offer_rows={1: {"27.0501": (2021, 2024)}},
    )
    merged = merge_cip_subfields(artifacts, feature_config)
    assert len(merged.rows) == 1
    row = merged.rows[0]
    assert row.strict_completions == 8
    assert row.strict_offers == ("27.0501",)
    assert row.strict_research["total_weighted"] == 0.0
    assert row.strict_research["primary_weighted"] == 0.0
    assert row.strict_research["years_with_activity"] == 0
    assert row.strict_research["avg_citation_impact"] is None


def test_merge_cip_subfields_empty_completions(conn: sqlite3.Connection) -> None:
    feature_config = validate_feature_config(_config(), conn)
    artifacts = _artifacts(
        completion_rows={},
        research_rows={
            1: [
                {
                    "subfield_id": 1103,
                    "year": 2023,
                    "publications_count": 5.0,
                    "avg_citation_impact": 1.0,
                }
            ]
        },
        offer_rows={1: {"26.0202": (2018, 2024)}},
    )
    merged = merge_cip_subfields(artifacts, feature_config)
    assert len(merged.rows) == 1
    row = merged.rows[0]
    assert row.strict_completions == 0
    assert row.family_completions == 0
    assert row.total_bachelors == 0
    assert row.total_bachelors_by_year == {year: 0 for year in range(2019, 2024)}
    assert row.strict_research["total_weighted"] == pytest.approx(5.0)


def test_merge_cip_subfields_orders_rows_by_university_then_cluster(
    conn: sqlite3.Connection,
) -> None:
    config = _config()
    config["field_clusters"].append(
        {
            "slug": "stats",
            "label": "Statistics",
            "cip_rules": {
                "include_6digit": ["27.0501"],
                "include_2digit_families": [],
                "exclude_6digit": [],
            },
            "fit_weights": {"adjacent": 0.25},
            "scoring_profile": "balanced",
        }
    )
    feature_config = validate_feature_config(config, conn)
    artifacts = _artifacts(
        universities=[{"university_id": 2}, {"university_id": 1}],
    )
    merged = merge_cip_subfields(artifacts, feature_config)
    assert [(row.university_id, row.cluster_slug) for row in merged.rows] == [
        (2, "molbio"),
        (2, "stats"),
        (1, "molbio"),
        (1, "stats"),
    ]


def test_compute_cluster_scores_weights_applied_correctly(conn: sqlite3.Connection) -> None:
    feature_config = validate_feature_config(_config(), conn)
    artifacts = _artifacts(
        completion_rows={
            1: [
                {"cip_code": "26.0202", "year": 2023, "awards_count": 12},
                {"cip_code": "26.0203", "year": 2023, "awards_count": 4},
                {"cip_code": "00.0099", "year": 2023, "awards_count": 200},
            ]
        },
        research_rows={
            1: [
                {
                    "subfield_id": 1103,
                    "year": 2023,
                    "publications_count": 5.0,
                    "avg_citation_impact": 1.5,
                }
            ]
        },
        offer_rows={1: {"26.0202": (2018, 2024), "26.0203": (2020, 2024)}},
    )
    merged = merge_cip_subfields(artifacts, feature_config)
    scored = compute_cluster_scores(merged, feature_config)

    assert len(scored.rows) == 1
    row = scored.rows[0]
    assert row.university_id == 1
    assert row.cluster_slug == "molbio"
    # direct/family completions: 12/200 and 16/200 over 5-year lookback.
    assert row.target_completions_share_direct == pytest.approx(12 / 200)
    assert row.target_completions_share_family == pytest.approx(16 / 200)
    # confidence_program = 0.25*1 + 0.35*min(16/max(20,20),1) + 0.20*min(12/10,1) + 0.20*min(7/10,1)
    expected_program = 0.25 + 0.35 * (16 / 20) + 0.20 * 1.0 + 0.20 * (7 / 10)
    assert row.confidence_program == pytest.approx(expected_program)
    # confidence_mapping = 0.65 * (2 mapped / 2 offered) + 0.35 * mean(weights={1103:1.0, 1110:0.6})
    expected_mapping = 0.65 * 1.0 + 0.35 * ((1.0 + 0.6) / 2)
    assert row.confidence_mapping == pytest.approx(expected_mapping)
    assert row.offers_any_target_cip == 1
    assert row.min_recent_bachelors_threshold_passed == 1
    assert row.eligible_for_ranking == 1
    assert row.exclusion_reasons == ()
    assert row.thin_completion_flag == 0
    assert row.sparse_mapping_flag == 0


def test_compute_cluster_scores_zero_value_inputs(conn: sqlite3.Connection) -> None:
    feature_config = validate_feature_config(_config(), conn)
    artifacts = _artifacts(
        completion_rows={},
        research_rows={},
        offer_rows={},
    )
    merged = merge_cip_subfields(artifacts, feature_config)
    scored = compute_cluster_scores(merged, feature_config)

    assert len(scored.rows) == 1
    row = scored.rows[0]
    assert row.target_completions_share_direct is None
    assert row.target_completions_share_family is None
    assert row.confidence_program == 0.0
    # No offers → confidence_mapping returns (0.0, sparse=1) before touching weights.
    assert row.confidence_mapping == 0.0
    assert row.sparse_mapping_flag == 1
    assert row.thin_completion_flag == 1
    assert row.offers_any_target_cip == 0
    assert row.min_recent_bachelors_threshold_passed == 0
    assert row.eligible_for_ranking == 0
    assert row.exclusion_reasons == ("no_target_offer",)


def test_compute_cluster_scores_handles_missing_completions_with_offer(
    conn: sqlite3.Connection,
) -> None:
    feature_config = validate_feature_config(_config(), conn)
    artifacts = _artifacts(
        completion_rows={},
        research_rows={},
        offer_rows={1: {"26.0202": (2018, 2024)}},
    )
    merged = merge_cip_subfields(artifacts, feature_config)
    scored = compute_cluster_scores(merged, feature_config)

    row = scored.rows[0]
    assert row.offers_any_target_cip == 1
    assert row.min_recent_bachelors_threshold_passed == 0
    assert row.eligible_for_ranking == 0
    assert row.exclusion_reasons == ("below_min_recent_bachelors_threshold",)
    # Shares are None when total_bachelors is zero — division never attempted.
    assert row.target_completions_share_direct is None
    assert row.target_completions_share_family is None


def test_compute_cluster_scores_single_university(conn: sqlite3.Connection) -> None:
    config = _config()
    config["field_clusters"].append(
        {
            "slug": "stats",
            "label": "Statistics",
            "cip_rules": {
                "include_6digit": ["27.0501"],
                "include_2digit_families": [],
                "exclude_6digit": [],
            },
            "fit_weights": {"adjacent": 0.25},
            "scoring_profile": "balanced",
        }
    )
    feature_config = validate_feature_config(config, conn)
    artifacts = _artifacts(
        universities=[{"university_id": 7}],
        completion_rows={
            7: [
                {"cip_code": "26.0202", "year": 2023, "awards_count": 10},
                {"cip_code": "00.0099", "year": 2023, "awards_count": 100},
            ]
        },
        research_rows={},
        offer_rows={7: {"26.0202": (2018, 2024)}},
    )
    merged = merge_cip_subfields(artifacts, feature_config)
    scored = compute_cluster_scores(merged, feature_config)

    assert [(row.university_id, row.cluster_slug) for row in scored.rows] == [
        (7, "molbio"),
        (7, "stats"),
    ]
    molbio = scored.rows[0]
    stats = scored.rows[1]
    assert molbio.eligible_for_ranking == 1
    assert molbio.exclusion_reasons == ()
    # No 27.0501 offers → "stats" excluded for that reason.
    assert stats.offers_any_target_cip == 0
    assert stats.eligible_for_ranking == 0
    assert stats.exclusion_reasons == ("no_target_offer",)


def _pipeline_inputs(
    tmp_path: Path,
) -> tuple[sqlite3.Connection, Any, FeatureArtifacts, list[dict[str, Any]]]:
    """Set up artifacts and assembled rows for write_feature_store tests."""

    conn = _make_artifacts_conn()
    _write_feature_parquet_fixtures(tmp_path)
    feature_config = validate_feature_config(_config(), conn)
    artifacts = load_feature_artifacts(feature_config, conn, tmp_path)
    rows = build_cluster_feature_rows(artifacts, feature_config, output_dir=tmp_path)
    return conn, feature_config, artifacts, rows


def test_write_feature_store_creates_parquet_with_expected_schema(tmp_path: Path) -> None:
    conn, feature_config, artifacts, built = _pipeline_inputs(tmp_path)
    try:
        rows, summary = write_feature_store(
            built, artifacts, feature_config, tmp_path, execute=True
        )
        feature_store_path = tmp_path / "feature_store.parquet"
        assert feature_store_path.is_file()
        table = pq.read_table(feature_store_path)
        assert table.num_rows == len(rows) == 1
        assert table.num_rows == len(artifacts.universities) * len(feature_config.clusters)
        column_names = set(table.column_names)
        for required in (
            "university_id",
            "field_cluster_slug",
            "scoring_profile",
            "offers_any_target_cip",
            "eligible_for_ranking",
            "exclusion_reason",
            "confidence_program",
            "confidence_research",
            "confidence_outcomes",
            "confidence_mapping",
            "confidence_identity",
            "source_openalex_run_dir",
        ):
            assert required in column_names, f"missing column {required}"
        first_row = table.to_pylist()[0]
        assert first_row["field_cluster_slug"] == "molbio"
        assert first_row["eligible_for_ranking"] == 1
        assert summary["feature_store_path"] == str(feature_store_path)
        assert summary["feature_store_rows"] == 1
        assert isinstance(summary["completion_lookback_years"], list)
    finally:
        conn.close()


def test_write_feature_store_overwrites_existing_output(tmp_path: Path) -> None:
    conn, feature_config, artifacts, built = _pipeline_inputs(tmp_path)
    try:
        feature_store_path = tmp_path / "feature_store.parquet"
        feature_store_path.write_bytes(b"stale-bytes")  # not a real parquet
        rows, _ = write_feature_store(built, artifacts, feature_config, tmp_path, execute=True)
        table = pq.read_table(feature_store_path)
        assert table.num_rows == len(rows) == 1
        assert table.to_pylist()[0]["field_cluster_slug"] == "molbio"
    finally:
        conn.close()


def test_write_feature_store_dry_run_skips_write(tmp_path: Path) -> None:
    conn, feature_config, artifacts, built = _pipeline_inputs(tmp_path)
    try:
        rows, summary = write_feature_store(
            built, artifacts, feature_config, tmp_path, execute=False
        )
        assert rows
        assert not (tmp_path / "feature_store.parquet").exists()
        # Summary still reports where the file would land so the manifest is consistent.
        assert summary["feature_store_path"] == str(tmp_path / "feature_store.parquet")
    finally:
        conn.close()


def test_pipeline_orchestrator_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive the five-function pipeline through the loader and verify output."""

    import build_field_cluster_features as bfcf

    monkeypatch.setattr(bfcf, "resolve_output_dir", lambda _config: tmp_path)
    conn = _make_artifacts_conn()
    try:
        _write_feature_parquet_fixtures(tmp_path)
        loader = BuildFieldClusterFeaturesLoader()
        args = type("Args", (), {"execute": True})()
        rows, manifest_summary = loader.load(
            conn=conn, config=_config(), args=args, snapshot="test"
        )

        assert len(rows) == 1
        row = rows[0]
        assert row["field_cluster_slug"] == "molbio"
        assert row["university_id"] == 1
        assert row["offers_any_target_cip"] == 1
        assert row["eligible_for_ranking"] == 1
        # Research/outcomes/identity confidences come from the writer's enrichment step.
        assert 0.0 <= row["confidence_research"] <= 1.0
        assert 0.0 <= row["confidence_outcomes"] <= 1.0
        assert 0.0 <= row["confidence_identity"] <= 1.0

        feature_store_path = tmp_path / "feature_store.parquet"
        assert feature_store_path.is_file()
        table = pq.read_table(feature_store_path)
        assert table.num_rows == 1
        assert table.to_pylist()[0]["university_id"] == 1

        assert manifest_summary["executed"] is True
        assert manifest_summary["eligible_rows"] == 1
        assert manifest_summary["feature_store_rows"] == 1
        assert manifest_summary["feature_store_path"] == str(feature_store_path)
        assert isinstance(manifest_summary["completion_lookback_years"], list), (
            "manifest lookback years must be a list so DataLoader._merge_manifest_summary "
            "flattens it to a count instead of leaking the years array"
        )
        assert manifest_summary["completion_lookback_years"] == [
            2019,
            2020,
            2021,
            2022,
            2023,
        ]
    finally:
        conn.close()
