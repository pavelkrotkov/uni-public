#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML>=6.0"]
# ///

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from shared.data_loader import ManualDataLoader

SAM_ENTITY_URL = "https://sam.gov/entity/{uei}/coreData?status=Active"


@dataclass(frozen=True)
class IdentifierCorrection:
    canonical_name: str
    uei: str
    duns: str | None
    source_url: str
    note: str


# Only corrections backed by institution-published sponsored-project identifier pages.
CORRECTIONS: tuple[IdentifierCorrection, ...] = (
    IdentifierCorrection(
        canonical_name="Claremont McKenna College",
        uei="L45FLFHWMGQ9",
        duns="009661893",
        source_url="https://www.cmc.edu/research/commonly-required-info",
        note="CMC grant proposal identifiers page",
    ),
    IdentifierCorrection(
        canonical_name="Columbia University in the City of New York",
        uei="F4N1QNPB95M4",
        duns="049179401",
        source_url="https://research.columbia.edu/sites/default/files/content/SPA/Forms/2023-09-05_Morningside_Attachment_3B__template.pdf",
        note="Columbia Morningside subrecipient template for The Trustees of Columbia University in the City of New York",
    ),
    IdentifierCorrection(
        canonical_name="Franklin W Olin College of Engineering",
        uei="DN8YL963MRP5",
        duns="133432760",
        source_url="https://www.olin.edu/sites/default/files/users/user501/olin_institutional_profile_September%202023.pdf",
        note="Olin institutional profile PDF",
    ),
    IdentifierCorrection(
        canonical_name="Pennsylvania State University-Main Campus",
        uei="NPM2J7MSCF61",
        duns=None,
        source_url="https://researchsupport.psu.edu/oras-knowledge-base/commonly-requested-information/",
        note="Penn State official research-support page publishes UEI but not DUNS",
    ),
    IdentifierCorrection(
        canonical_name="Stony Brook University",
        uei="MXLDJ7JKMBE1",
        duns="789728300",
        source_url="https://www.stonybrook.edu/commcms/accounting/resources/University_ID_Numbers_and_Codes.php",
        note="Stony Brook controller page",
    ),
    IdentifierCorrection(
        canonical_name="University at Albany",
        uei="G27VCK35DJB3",
        duns="152652822",
        source_url="https://www.albany.edu/research-economic-development/researcher-guidance/award-lifecycle/proposal-preparation-submission",
        note="UAlbany proposal preparation page",
    ),
    IdentifierCorrection(
        canonical_name="University of California-Los Angeles",
        uei="RN64EPNH8JC6",
        duns="092530369",
        source_url="https://ocga.research.ucla.edu/ucla-standard-info/",
        note="UCLA standard information page",
    ),
    IdentifierCorrection(
        canonical_name="University of California-Santa Cruz",
        uei="VXUFPE4MCZH5",
        duns="125084723",
        source_url="https://officeofresearch.ucsc.edu/institutional-information/",
        note="UCSC institutional information page",
    ),
    IdentifierCorrection(
        canonical_name="University of Colorado Boulder",
        uei="SPVKK1RC2MZ3",
        duns="007431505",
        source_url="https://www.colorado.edu/ocg/frequently-needed-information",
        note="CU Boulder OCG frequently needed information",
    ),
    IdentifierCorrection(
        canonical_name="University of Kansas",
        uei="WGK8J9FEKW65",
        duns="007180078",
        source_url="https://research.ku.edu/federal-identifiers",
        note="KU federal identifiers page; using University of Kansas entry, not KUCR",
    ),
    IdentifierCorrection(
        canonical_name="University of Michigan-Ann Arbor",
        uei="GNJ7BBP73WE9",
        duns="073133571",
        source_url="https://orsp.umich.edu/develop-proposal/frequently-required-proposal-data-documents",
        note="U-M frequently required proposal data",
    ),
    IdentifierCorrection(
        canonical_name="University of Wisconsin-Madison",
        uei="LCLSJAGTNZQ7",
        duns="161202122",
        source_url="https://rsp.wisc.edu/factualdata.cfm",
        note="UW-Madison Research and Sponsored Programs factual data page",
    ),
)


def ensure_systems(conn: sqlite3.Connection) -> None:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='university_ueis'"
    ).fetchone()
    if not exists:
        raise RuntimeError("university_ueis table not found — run align_schema.py --execute first")


def upsert_identifier(
    conn: sqlite3.Connection,
    *,
    university_id: int,
    system_code: str,
    external_id: str,
    external_url: str | None,
) -> None:
    if system_code != "UEI":
        return  # DUNS no longer stored; only UEI is relevant
    from datetime import UTC, datetime

    now_iso = datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT INTO university_ueis (university_id, uei, entity_name, entity_role,
                                     is_primary, discovery_source, discovered_at, notes)
        VALUES (?, ?, NULL, 'main', 1, 'manual', ?, NULL)
        ON CONFLICT(university_id, uei) DO UPDATE SET
            is_primary = 1,
            discovery_source = 'manual',
            discovered_at = excluded.discovered_at
        """,
        (university_id, external_id, now_iso),
    )


def load_university_ids(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        row["canonical_name"]: row["university_id"]
        for row in conn.execute("SELECT university_id, canonical_name FROM universities")
    }


def current_identifier(
    conn: sqlite3.Connection, *, university_id: int, system_code: str
) -> tuple[str, str | None] | None:
    if system_code != "UEI":
        return None  # DUNS no longer stored
    row = conn.execute(
        "SELECT uei FROM university_ueis WHERE university_id = ? AND is_primary = 1",
        (university_id,),
    ).fetchone()
    if row is None:
        return None
    return (row["uei"], None)


def apply_corrections(conn: sqlite3.Connection) -> list[str]:
    university_ids = load_university_ids(conn)
    missing_names = [
        c.canonical_name for c in CORRECTIONS if c.canonical_name not in university_ids
    ]
    if missing_names:
        raise RuntimeError(f"Missing universities: {', '.join(missing_names)}")

    changes: list[str] = []
    for correction in CORRECTIONS:
        university_id = university_ids[correction.canonical_name]

        before_uei = current_identifier(conn, university_id=university_id, system_code="UEI")
        if before_uei is None or before_uei[0] != correction.uei:
            changes.append(
                f"{correction.canonical_name}: UEI {before_uei[0] if before_uei else '<missing>'} -> {correction.uei}"
            )
        upsert_identifier(
            conn,
            university_id=university_id,
            system_code="UEI",
            external_id=correction.uei,
            external_url=None,
        )

    return changes


def universities_without_primary_uei(conn: sqlite3.Connection) -> list[str]:
    return [
        row["canonical_name"]
        for row in conn.execute(
            """
            SELECT u.canonical_name
            FROM universities u
            LEFT JOIN university_ueis uu
              ON uu.university_id = u.university_id
             AND uu.is_primary = 1
            WHERE uu.university_id IS NULL
            ORDER BY u.canonical_name
            """
        )
    ]


class SecondPassUeiDunsLoader(ManualDataLoader):
    source_code = "SECOND_PASS_UEI_DUNS"
    description = "Apply curated second-pass UEI/DUNS corrections to specific universities."
    requires_config = False
    execute_help = "write changes to the live SQLite database (default is dry-run)"

    def load(
        self,
        *,
        conn: sqlite3.Connection | None,
        config: dict[str, Any],
        args,
        snapshot: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        conn = self._require_conn(conn)
        ensure_systems(conn)
        changes = apply_corrections(conn)
        unresolved = universities_without_primary_uei(conn)

        if args.execute:
            conn.commit()
        else:
            conn.rollback()
        return [], {
            "executed": args.execute,
            "db": str(args.db),
            "identifier_changes": len(changes),
            "universities_without_primary_uei": len(unresolved),
            "curated_sources": len(CORRECTIONS),
            "changes": changes,
            "unresolved": unresolved,
        }


if __name__ == "__main__":
    raise SystemExit(SecondPassUeiDunsLoader().run())
