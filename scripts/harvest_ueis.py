#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML>=6.0"]
# ///

"""harvest_ueis.py — Discover all UEIs per university and populate university_ueis.

Runs three discovery phases:

  Phase A: SAM.gov
    Reuses Resolver and alias_variants from add_uei_duns.py.
    Keeps ALL candidates scoring >= 50 (not just the best).
    Infers entity_role from the SAM.gov legal name.

  Phase B: NIH RePORTER
    Name-alias search; collects org_ueis from all matched projects.
    Best source for medical-center UEIs that hold NIH grants.

  Phase C: NSF Awards API
    Name-alias search; collects ueiNumber and parentUeiNumber.
    Adds any additional entity UEIs not discovered in A/B.

Cross-source deduplication: if a UEI is discovered by multiple sources,
the first source wins; later discoveries append to the notes field.

Usage:
    uv run scripts/harvest_ueis.py [--execute] [--db PATH]
    uv run scripts/harvest_ueis.py --university-id 54  # NYU only

Default is dry-run. Older write-by-default cron calls must add --execute.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from shared.data_loader import ManualDataLoader
from shared.http import JsonSource

NIH_PROJECTS_URL = "https://api.reporter.nih.gov/v2/projects/search"
NSF_AWARDS_URL = "https://api.nsf.gov/services/v1/awards.json"
HEADERS = {"User-Agent": "univ-harvest-ueis/1.0", "Content-Type": "application/json"}

# Keywords used to infer entity_role from legal name
MEDICAL_KEYWORDS = {
    "HOSPITAL",
    "HEALTH",
    "MEDICAL",
    "MEDICINE",
    "CLINIC",
    "LANGONE",
    "PRESBYTERIAN",
}
FOUNDATION_KEYWORDS = {"FOUNDATION", "FUND", "ENDOWMENT"}
MAIN_KEYWORDS = {"UNIVERSITY", "COLLEGE", "INSTITUTE", "SCHOOL", "TRUSTEES", "REGENTS", "BOARD"}

# Minimum score for a SAM.gov candidate to be included as a related UEI
SAM_SCORE_THRESHOLD = 50


def infer_entity_role(name: str) -> str:
    upper = name.upper()
    words = set(upper.split())
    if words & MEDICAL_KEYWORDS:
        return "medical"
    if words & FOUNDATION_KEYWORDS:
        return "foundation"
    if words & MAIN_KEYWORDS:
        return "main"
    return "other"


@dataclass
class DiscoveredUEI:
    uei: str
    entity_name: str | None
    entity_role: str
    discovery_source: str
    extra_sources: list[str] = field(default_factory=list)

    def notes_str(self) -> str | None:
        if not self.extra_sources:
            return None
        return "also confirmed by: " + ", ".join(self.extra_sources)


# ---------------------------------------------------------------------------
# Phase A: SAM.gov
# ---------------------------------------------------------------------------


def harvest_sam(
    source: JsonSource,
    university_id: int,
    canonical_name: str,
    ror_id: str | None,
    primary_uei: str | None,
) -> dict[str, DiscoveredUEI]:
    """Return all SAM.gov UEIs scoring >= SAM_SCORE_THRESHOLD for this university."""
    from add_uei_duns import Resolver, alias_variants, score_name

    resolver = Resolver(source)
    aliases = alias_variants(canonical_name)
    aliases.extend(resolver.ror_names(ror_id))
    aliases = list(dict.fromkeys(a for a in aliases if a and len(a) >= 3))

    candidates: dict[str, dict] = {}
    for query in aliases[:12]:
        try:
            for row in resolver.sam_search_name(query):
                uei = row["ueiSAM"]
                candidates.setdefault(
                    uei,
                    {"uei": uei, "sam_name": row["legalBusinessName"], "sources": set()},
                )["sources"].add("sam_name")
        except Exception:
            continue

    try:
        for org in resolver.nih_orgs(aliases):
            sam = resolver.sam_by_uei(org["uei"])
            if not sam:
                continue
            entry = candidates.setdefault(
                org["uei"],
                {"uei": org["uei"], "sam_name": sam["legalBusinessName"], "sources": set()},
            )
            entry["sources"].add("nih")
            if org.get("nih_name"):
                entry["nih_name"] = org["nih_name"]
    except Exception:
        pass

    results: dict[str, DiscoveredUEI] = {}
    for entry in candidates.values():
        score = max(
            score_name(entry.get("sam_name"), aliases),
            score_name(entry.get("nih_name"), aliases),
        )
        if "nih" in entry["sources"]:
            score += 20
        if entry["uei"] == primary_uei:
            score += 25
        if score < SAM_SCORE_THRESHOLD:
            continue
        name = entry.get("sam_name") or entry.get("nih_name") or ""
        results[entry["uei"]] = DiscoveredUEI(
            uei=entry["uei"],
            entity_name=name or None,
            entity_role=infer_entity_role(name),
            discovery_source="sam_gov",
        )
    return results


# ---------------------------------------------------------------------------
# Phase B: NIH RePORTER
# ---------------------------------------------------------------------------


def harvest_nih(
    source: JsonSource,
    canonical_name: str,
    aliases: tuple[str, ...],
) -> dict[str, DiscoveredUEI]:
    """Return all UEIs seen in NIH projects matched to this university."""
    from add_uei_duns import normalize

    alias_list = list(aliases[:8])
    if canonical_name not in alias_list:
        alias_list.insert(0, canonical_name)
    alias_list = list(dict.fromkeys(alias_list))[:8]

    try:
        payload = {
            "criteria": {"org_names": alias_list},
            "include_fields": ["Organization"],
            "limit": 500,
            "offset": 0,
        }
        data = source.fetch(NIH_PROJECTS_URL, payload=payload)
    except Exception:
        return {}

    alias_norms = {normalize(a) for a in aliases if a}

    results: dict[str, DiscoveredUEI] = {}
    for project in data.get("results", []):
        org = project.get("organization") or {}
        org_name = org.get("org_name") or ""
        org_norm = normalize(org_name)
        # Only include projects whose org name matches one of our aliases
        if org_norm not in alias_norms and not any(a and a in org_norm for a in alias_norms):
            continue
        all_ueis = [org.get("primary_uei"), *(org.get("org_ueis") or [])]
        for uei in all_ueis:
            if not uei:
                continue
            uei = uei.strip()
            if uei not in results:
                results[uei] = DiscoveredUEI(
                    uei=uei,
                    entity_name=org_name or None,
                    entity_role=infer_entity_role(org_name),
                    discovery_source="nih_reporter",
                )
    return results


# ---------------------------------------------------------------------------
# Phase C: NSF Awards API
# ---------------------------------------------------------------------------


def harvest_nsf(
    source: JsonSource,
    canonical_name: str,
    state_code: str | None,
    aliases: tuple[str, ...],
    start_year: int = 2020,
    end_year: int = 2024,
) -> dict[str, DiscoveredUEI]:
    """Return all UEIs seen in NSF awards matched to this university."""
    from add_uei_duns import normalize

    if not state_code:
        return {}

    alias_norms = {normalize(a) for a in aliases if a}
    results: dict[str, DiscoveredUEI] = {}

    for alias in list(aliases[:8]):
        try:
            data = source.fetch(
                NSF_AWARDS_URL,
                params={
                    "awardeeName": alias,
                    "awardeeStateCode": state_code,
                    "dateStart": f"01/01/{start_year}",
                    "dateEnd": f"12/31/{end_year}",
                    "fields": "awardeeName,ueiNumber,parentUeiNumber,awardeeStateCode",
                    "printFields": "awardeeName,ueiNumber,parentUeiNumber,awardeeStateCode",
                },
            )
        except Exception:
            continue

        for award in data.get("response", {}).get("award") or []:
            awardee_state = award.get("awardeeStateCode")
            if state_code and awardee_state and state_code != awardee_state:
                continue
            awardee_name = award.get("awardeeName") or ""
            awardee_norm = normalize(awardee_name)
            if awardee_norm not in alias_norms and not any(
                a and a in awardee_norm for a in alias_norms
            ):
                continue
            for uei_field, role_hint in [("ueiNumber", None), ("parentUeiNumber", "parent")]:
                uei = (award.get(uei_field) or "").strip()
                if not uei:
                    continue
                if uei not in results:
                    role = role_hint or infer_entity_role(awardee_name)
                    results[uei] = DiscoveredUEI(
                        uei=uei,
                        entity_name=awardee_name or None,
                        entity_role=role,
                        discovery_source="nsf_awards",
                    )
        time.sleep(0.05)

    return results


# ---------------------------------------------------------------------------
# Merge and write
# ---------------------------------------------------------------------------


def merge_discoveries(*source_dicts: dict[str, DiscoveredUEI]) -> dict[str, DiscoveredUEI]:
    merged: dict[str, DiscoveredUEI] = {}
    for source in source_dicts:
        for uei, disc in source.items():
            if uei not in merged:
                merged[uei] = disc
            else:
                # Already seen — record additional source in notes
                merged[uei].extra_sources.append(disc.discovery_source)
    return merged


def upsert_ueis(
    conn: sqlite3.Connection,
    university_id: int,
    primary_uei: str | None,
    discoveries: dict[str, DiscoveredUEI],
    now_iso: str,
) -> int:
    inserted = 0
    for uei, disc in discoveries.items():
        is_primary = 1 if uei == primary_uei else 0
        notes = disc.notes_str()
        existing = conn.execute(
            "SELECT notes FROM university_ueis WHERE university_id = ? AND uei = ?",
            (university_id, uei),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO university_ueis
                    (university_id, uei, entity_name, entity_role, is_primary,
                     discovery_source, discovered_at, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    university_id,
                    uei,
                    disc.entity_name,
                    disc.entity_role,
                    is_primary,
                    disc.discovery_source,
                    now_iso,
                    notes,
                ),
            )
            inserted += 1
        elif notes:
            # Update notes only if we have new cross-source confirmations
            existing_notes = existing["notes"] or ""
            merged_notes = existing_notes + "; " + notes if existing_notes else notes
            conn.execute(
                "UPDATE university_ueis SET notes = ? WHERE university_id = ? AND uei = ?",
                (merged_notes, university_id, uei),
            )
    return inserted


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


@dataclass
class UniversityRecord:
    university_id: int
    canonical_name: str
    state_code: str | None
    ror_id: str | None
    primary_uei: str | None
    aliases: tuple[str, ...]


def load_universities(conn: sqlite3.Connection, requested_ids: list[int]) -> list[UniversityRecord]:
    from add_uei_duns import alias_variants

    rows = conn.execute(
        """
        SELECT
            u.university_id,
            u.canonical_name,
            u.state_code,
            MAX(CASE WHEN ui.system_code = 'ROR' THEN ui.external_id END) AS ror_id,
            MAX(CASE WHEN uu.is_primary = 1 THEN uu.uei END) AS primary_uei
        FROM universities u
        LEFT JOIN university_identifiers ui ON ui.university_id = u.university_id
        LEFT JOIN university_ueis uu ON uu.university_id = u.university_id
        GROUP BY u.university_id
        ORDER BY u.university_id
        """
    ).fetchall()
    records = [
        UniversityRecord(
            university_id=row["university_id"],
            canonical_name=row["canonical_name"],
            state_code=row["state_code"],
            ror_id=row["ror_id"],
            primary_uei=row["primary_uei"],
            aliases=tuple(dict.fromkeys(alias_variants(row["canonical_name"]))),
        )
        for row in rows
    ]
    if requested_ids:
        requested = set(requested_ids)
        records = [r for r in records if r.university_id in requested]
    return records


class HarvestUeisLoader(ManualDataLoader):
    source_code = "HARVEST_UEIS"
    description = "Harvest all UEIs per university from SAM.gov, NIH, and NSF."
    requires_config = False
    execute_help = "write harvested UEIs to SQLite (default is dry-run)"
    supports_university_id = True
    requires_json_source = True
    json_source_headers = HEADERS
    # Phase B (NIH, limit=500) is the slowest call; a timeout is a ceiling, not
    # a wait, so one default covering the slowest phase suits all three.
    default_request_timeout = 45

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--phases",
            default="ABC",
            help="phases to run (default: ABC); e.g. --phases A or --phases AB",
        )

    def load(
        self,
        *,
        conn: sqlite3.Connection | None,
        config: dict[str, Any],
        args,
        snapshot: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        conn = self._require_conn(conn)
        phases = args.phases.upper()
        universities = load_universities(conn, args.university_id)
        now_iso = datetime.now(UTC).isoformat()
        total_inserted = 0
        total_discoveries = 0

        for idx, univ in enumerate(universities, 1):
            print(
                f"[{idx:03d}/{len(universities):03d}] {univ.university_id} {univ.canonical_name}",
                flush=True,
            )

            sam_ueis: dict[str, DiscoveredUEI] = {}
            nih_ueis: dict[str, DiscoveredUEI] = {}
            nsf_ueis: dict[str, DiscoveredUEI] = {}

            if "A" in phases:
                try:
                    sam_ueis = harvest_sam(
                        self.json_source,
                        univ.university_id,
                        univ.canonical_name,
                        univ.ror_id,
                        univ.primary_uei,
                    )
                except Exception as exc:
                    print(f"  SAM.gov error: {exc}")

            if "B" in phases:
                try:
                    nih_ueis = harvest_nih(self.json_source, univ.canonical_name, univ.aliases)
                except Exception as exc:
                    print(f"  NIH error: {exc}")

            if "C" in phases:
                try:
                    nsf_ueis = harvest_nsf(
                        self.json_source, univ.canonical_name, univ.state_code, univ.aliases
                    )
                except Exception as exc:
                    print(f"  NSF error: {exc}")

            all_discoveries = merge_discoveries(sam_ueis, nih_ueis, nsf_ueis)
            total_discoveries += len(all_discoveries)
            new_count = len(all_discoveries) - 1  # exclude primary which already exists

            print(
                f"  sam={len(sam_ueis)} nih={len(nih_ueis)} nsf={len(nsf_ueis)} "
                f"merged={len(all_discoveries)} new_candidates={max(new_count, 0)}"
            )

            for uei, disc in sorted(all_discoveries.items()):
                marker = "*" if uei == univ.primary_uei else " "
                print(
                    f"  {marker} {uei} [{disc.entity_role}] {disc.entity_name or '?'} "
                    f"({disc.discovery_source})"
                )

            if args.execute and all_discoveries:
                inserted = upsert_ueis(
                    conn, univ.university_id, univ.primary_uei, all_discoveries, now_iso
                )
                total_inserted += inserted

        if args.execute:
            conn.commit()
        else:
            conn.rollback()
        return [], {
            "executed": args.execute,
            "db": str(args.db),
            "phases": phases,
            "universities": len(universities),
            "discoveries": total_discoveries,
            "inserted": total_inserted,
        }


if __name__ == "__main__":
    raise SystemExit(HarvestUeisLoader().run())
