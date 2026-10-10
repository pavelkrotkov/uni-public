#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "PyYAML>=6.0",
#   "pyarrow>=20.0.0",
# ]
# ///

from __future__ import annotations

from collections import defaultdict
from typing import Any

from shared.config import resolve_output_dir
from shared.data_loader import ManualDataLoader
from shared.io_utils import load_parquet_rows, write_csv


def assign_profile_ranks(rows: list[dict[str, Any]], score_key: str) -> None:
    by_cluster: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_cluster[row["field_cluster_slug"]].append(row)
    for cluster_rows in by_cluster.values():
        cluster_rows.sort(
            key=lambda row: (
                float(row[score_key]),
                float(row["confidence_score"]),
                float(row["research_opportunity_score"]),
                float(row["recent_bachelors_completions_family"]),
                row["canonical_name"],
            ),
            reverse=True,
        )
        rank_key = score_key.replace("overall_", "").replace("_score", "") + "_rank"
        for index, row in enumerate(cluster_rows, start=1):
            row[rank_key] = index


def build_sensitivity_rows(rankings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    working = [dict(row) for row in rankings]
    assign_profile_ranks(working, "overall_research_heavy_score")
    assign_profile_ranks(working, "overall_value_heavy_score")
    sensitivity_rows = []
    for row in sorted(working, key=lambda item: (item["field_cluster_slug"], int(item["rank"]))):
        research_rank = int(row["research_heavy_rank"])
        value_rank = int(row["value_heavy_rank"])
        balanced_rank = int(row["rank"])
        sensitivity_rows.append(
            {
                "field_cluster_slug": row["field_cluster_slug"],
                "rank_balanced": balanced_rank,
                "rank_research_heavy": research_rank,
                "delta_research_heavy": research_rank - balanced_rank,
                "rank_value_heavy": value_rank,
                "delta_value_heavy": value_rank - balanced_rank,
                "max_abs_rank_delta": max(
                    abs(research_rank - balanced_rank),
                    abs(value_rank - balanced_rank),
                ),
                "university_id": row["university_id"],
                "canonical_name": row["canonical_name"],
                "overall_balanced_score": row["overall_balanced_score"],
                "overall_research_heavy_score": row["overall_research_heavy_score"],
                "overall_value_heavy_score": row["overall_value_heavy_score"],
                "confidence_score": row["confidence_score"],
            }
        )
    return sensitivity_rows


def _review_row(
    issue_type: str,
    row: dict[str, Any],
    ranked: dict[str, Any] | None,
    issue_detail: str,
    suggested_action: str,
) -> dict[str, Any]:
    return {
        "issue_type": issue_type,
        "field_cluster_slug": row["field_cluster_slug"],
        "rank": ranked["rank"] if ranked else None,
        "university_id": row["university_id"],
        "canonical_name": row["canonical_name"],
        "confidence_score": ranked["confidence_score"] if ranked else None,
        "issue_detail": issue_detail,
        "suggested_action": suggested_action,
        "unmapped_offered_cips": row.get("family_unmapped_offered_cips", ""),
        "effective_outcomes_source": row.get("effective_outcomes_source", ""),
        "nih_total_5y": row.get("nih_total_5y"),
        "federal_grants_total_5y": row.get("federal_grants_total_5y"),
    }


def build_review_queue(
    feature_store: list[dict[str, Any]], rankings: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    review_rows: list[dict[str, Any]] = []
    ranked_index = {(int(row["university_id"]), row["field_cluster_slug"]): row for row in rankings}

    # Top-25 entries with low confidence
    for row in rankings:
        if int(row["rank"]) <= 25 and float(row["confidence_score"]) < 0.5:
            review_rows.append(
                _review_row(
                    "top25_low_confidence",
                    row,
                    row,
                    "Top-25 rank with confidence below 0.5",
                    "Manually verify data completeness; check for missing scorecard or completions data",
                )
            )

    # Offered CIPs with no CIP-to-subfield mapping
    for row in feature_store:
        if int(row.get("family_unmapped_offered_cip_count", 0)) <= 0:
            continue
        ranked = ranked_index.get((int(row["university_id"]), row["field_cluster_slug"]))
        count = int(row["family_unmapped_offered_cip_count"])
        review_rows.append(
            _review_row(
                "unmapped_offered_cips",
                row,
                ranked,
                f"{count} offered target CIP(s) lack CIP-to-subfield mappings",
                "Add mappings in cip_to_openalex_subfields_openalex_native.md, then re-run expand_statistics_cips.py or bootstrap_db.py",
            )
        )

    # Eligible schools whose research scoring depends on sparse mappings
    for row in feature_store:
        if int(row.get("sparse_mapping_flag", 0)) != 1:
            continue
        if int(row.get("eligible_for_ranking", 0)) != 1:
            continue
        ranked = ranked_index.get((int(row["university_id"]), row["field_cluster_slug"]))
        if not ranked:
            continue
        review_rows.append(
            _review_row(
                "sparse_mapping",
                row,
                ranked,
                "Eligible row depends on sparse target-subfield mappings",
                "Extend CIP-to-subfield coverage for this cluster or verify that the mapping gap is intentional",
            )
        )

    # Schools with active research output but zero NIH funding (likely a matching failure)
    seen_nih_issue: set[int] = set()
    for row in feature_store:
        university_id = int(row["university_id"])
        if university_id in seen_nih_issue:
            continue
        nih_total = float(row.get("nih_total_5y") or 0.0)
        family_pubs = float(row.get("research_publications_family_weighted_5y") or 0.0)
        usaspending = float(row.get("usaspending_total_5y") or 0.0)
        if nih_total > 0 or family_pubs < 10.0:
            continue
        if usaspending == 0.0:
            continue  # zero across all sources — likely a non-research school, not a matching failure
        seen_nih_issue.add(university_id)
        ranked = ranked_index.get((university_id, row["field_cluster_slug"]))
        review_rows.append(
            _review_row(
                "missing_nih_data",
                row,
                ranked,
                f"Active research (family pubs={family_pubs:.0f}) but $0 NIH over 5 years; USAspending=${usaspending:,.0f}",
                "Re-run load_nih_awards.py --university-id <id> --execute; verify data/identity_overrides.json aliases",
            )
        )

    # Eligible schools with no grant context at all
    seen_zero_grants: set[int] = set()
    for row in feature_store:
        if int(row.get("eligible_for_ranking", 0)) != 1:
            continue
        university_id = int(row["university_id"])
        if university_id in seen_zero_grants:
            continue
        if float(row.get("federal_grants_total_5y") or 0.0) > 0:
            continue
        seen_zero_grants.add(university_id)
        ranked = ranked_index.get((university_id, row["field_cluster_slug"]))
        review_rows.append(
            _review_row(
                "zero_grants",
                row,
                ranked,
                "Eligible for ranking but no federal grant data from any source",
                "Check USAspending, NIH, and NSF load manifests; verify UEI is populated in university_identifiers",
            )
        )

    # Schools that offer a target CIP but have zero family completions in the lookback window
    for row in feature_store:
        if not int(row.get("offers_any_target_cip", 0)):
            continue
        if int(row.get("recent_bachelors_completions_family", 0)) > 0:
            continue
        if int(row.get("eligible_for_ranking", 0)) == 1:
            continue  # already ranked — would be noise; the threshold handles it
        ranked = ranked_index.get((int(row["university_id"]), row["field_cluster_slug"]))
        review_rows.append(
            _review_row(
                "offer_completion_mismatch",
                row,
                ranked,
                "Offers target-family CIP but zero completions in lookback window",
                "Verify IPEDS unitid is correct; check completions data for recent years; confirm program is still active",
            )
        )

    review_rows.sort(
        key=lambda row: (
            row["issue_type"],
            row["field_cluster_slug"],
            row["rank"] if row["rank"] is not None else 9_999,
            row["canonical_name"],
        )
    )
    return review_rows


def coverage_summary(
    feature_store: list[dict[str, Any]], rankings: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    by_cluster: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in feature_store:
        by_cluster[row["field_cluster_slug"]].append(row)

    for cluster_slug, rows in sorted(by_cluster.items()):
        eligible = [row for row in rows if int(row["eligible_for_ranking"]) == 1]
        ranked = [row for row in rankings if row["field_cluster_slug"] == cluster_slug]
        summaries.append(
            {
                "field_cluster_slug": cluster_slug,
                "feature_rows": len(rows),
                "eligible_rows": len(eligible),
                "ranked_rows": len(ranked),
                "eligible_rate": len(eligible) / len(rows) if rows else 0.0,
                "direct_program_match_rate": sum(
                    int(row["offered_direct_cip_count"] > 0) for row in eligible
                )
                / len(eligible)
                if eligible
                else 0.0,
                "field_outcome_available_rate": sum(
                    int(row["field_outcome_available"]) for row in eligible
                )
                / len(eligible)
                if eligible
                else 0.0,
                "institution_fallback_rate": sum(
                    int(row["using_institution_fallback"]) for row in eligible
                )
                / len(eligible)
                if eligible
                else 0.0,
                "research_active_rate": sum(
                    int(float(row["research_publications_family_weighted_5y"]) > 0)
                    for row in eligible
                )
                / len(eligible)
                if eligible
                else 0.0,
                "grant_context_rate": sum(
                    int(float(row["federal_grants_total_5y"]) > 0) for row in eligible
                )
                / len(eligible)
                if eligible
                else 0.0,
                "low_confidence_top25_count": sum(
                    1
                    for row in ranked
                    if int(row["rank"]) <= 25 and float(row["confidence_score"]) < 0.5
                ),
                "unmapped_offered_cip_rows": sum(
                    int(row["family_unmapped_offered_cip_count"] > 0) for row in eligible
                ),
                "median_confidence": (
                    sorted(float(row["confidence_score"]) for row in ranked)[len(ranked) // 2]
                    if ranked
                    else 0.0
                ),
            }
        )
    return summaries


def format_pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def build_markdown_report(
    coverage_rows: list[dict[str, Any]],
    rankings: list[dict[str, Any]],
    review_rows: list[dict[str, Any]],
    sensitivity_rows: list[dict[str, Any]],
) -> str:
    lines = ["# Phase 3 QA Report", ""]
    lines.append("## Coverage summary")
    lines.append("")
    lines.append(
        "| Cluster | Ranked | Eligible rate | Direct program match | Field outcomes | Institution fallback | Research active | Grant context | Low-conf top25 | Unmapped offered CIPs | Median confidence |"
    )
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for row in coverage_rows:
        lines.append(
            "| "
            + f"{row['field_cluster_slug']} | "
            + f"{row['ranked_rows']} | "
            + f"{format_pct(row['eligible_rate'])} | "
            + f"{format_pct(row['direct_program_match_rate'])} | "
            + f"{format_pct(row['field_outcome_available_rate'])} | "
            + f"{format_pct(row['institution_fallback_rate'])} | "
            + f"{format_pct(row['research_active_rate'])} | "
            + f"{format_pct(row['grant_context_rate'])} | "
            + f"{row['low_confidence_top25_count']} | "
            + f"{row['unmapped_offered_cip_rows']} | "
            + f"{row['median_confidence']:.3f} |"
        )

    lines.extend(["", "## Top 10 by cluster", ""])
    by_cluster: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rankings:
        by_cluster[row["field_cluster_slug"]].append(row)
    for cluster_slug in sorted(by_cluster):
        lines.append(f"### {cluster_slug}")
        lines.append("")
        for row in by_cluster[cluster_slug][:10]:
            lines.append(
                f"- #{row['rank']} {row['canonical_name']} "
                f"(score {row['overall_balanced_score']:.2f}, confidence {row['confidence_score']:.3f})"
            )
        lines.append("")

    lines.extend(["## Sensitivity summary", ""])
    by_cluster_sensitivity: dict[str, list[int]] = defaultdict(list)
    for row in sensitivity_rows:
        by_cluster_sensitivity[row["field_cluster_slug"]].append(int(row["max_abs_rank_delta"]))
    for cluster_slug in sorted(by_cluster_sensitivity):
        deltas = sorted(by_cluster_sensitivity[cluster_slug])
        median_delta = deltas[len(deltas) // 2]
        max_delta = max(deltas) if deltas else 0
        lines.append(
            f"- {cluster_slug}: median max rank delta {median_delta}, max observed delta {max_delta}"
        )

    lines.extend(["", "## Review queue summary", ""])
    review_counts: dict[str, int] = defaultdict(int)
    for row in review_rows:
        review_counts[row["issue_type"]] += 1
    if review_counts:
        for issue_type, count in sorted(review_counts.items()):
            lines.append(f"- {issue_type}: {count}")
    else:
        lines.append("- No review items generated.")
    lines.append("")
    return "\n".join(lines)


class QACoverageLoader(ManualDataLoader):
    source_code = "QA_COVERAGE"
    description = "Generate Stage 11 QA outputs from feature store and rankings."
    execute_help = "write qa_report.md, sensitivity_report.csv, and review_queue.csv"

    def load(
        self,
        *,
        conn,
        config: dict[str, Any],
        args,
        snapshot: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        output_dir = resolve_output_dir(config)
        feature_store = load_parquet_rows(output_dir / "feature_store.parquet")
        rankings = load_parquet_rows(output_dir / "rankings_balanced.parquet")
        sensitivity_rows = build_sensitivity_rows(rankings)
        review_rows = build_review_queue(feature_store, rankings)
        coverage_rows = coverage_summary(feature_store, rankings)
        report = build_markdown_report(coverage_rows, rankings, review_rows, sensitivity_rows)

        qa_report_path = output_dir / "qa_report.md"
        sensitivity_path = output_dir / "sensitivity_report.csv"
        review_queue_path = output_dir / "review_queue.csv"

        if args.execute:
            qa_report_path.write_text(report + "\n")
            write_csv(sensitivity_path, sensitivity_rows)
            write_csv(review_queue_path, review_rows)

        return coverage_rows, {
            "executed": args.execute,
            "qa_report_path": str(qa_report_path),
            "sensitivity_path": str(sensitivity_path),
            "review_queue_path": str(review_queue_path),
            "coverage_rows": len(coverage_rows),
            "sensitivity_rows": len(sensitivity_rows),
            "review_queue_rows": len(review_rows),
        }


if __name__ == "__main__":
    raise SystemExit(QACoverageLoader().run())
