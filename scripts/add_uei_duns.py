#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML>=6.0"]
# ///

import sqlite3
import sys
import time
from typing import Any

from identity_overrides import alias_variants, get_manual_uei, normalize
from shared.data_loader import ManualDataLoader
from shared.http import JsonSource

SAM_ENDPOINT = "https://sam.gov/api/prod/entity-information/v1/api/entity-picker-data"
ROR_ENDPOINT = "https://api.ror.org/v2/organizations/"
NIH_ENDPOINT = "https://api.reporter.nih.gov/v2/projects/search"

HEADERS = {"User-Agent": "Mozilla/5.0"}

STOPWORDS = {"THE", "OF", "AND", "AT", "FOR", "IN", "ON", "A", "AN", "TO"}
HARD_PENALTIES = {
    "HILLEL": -50,
    "POLICE": -50,
    "FOUNDATION": -30,
    "HOSPITAL": -25,
    "PHYSICIANS": -22,
    "ALUMNI": -20,
    "ATHLETIC": -18,
    "ASSOCIATION": -14,
    "SOCIETY": -12,
    "MEMORIAL": -10,
}
SOFT_PENALTIES = {
    "MEDICAL": -8,
    "HEALTH": -8,
    "CAMPUS": -4,
    "CENTER": -4,
    "RESEARCH": -4,
    "HOLDINGS": -20,
    "PROPERTIES": -20,
}
GOOD_WORDS = {
    "UNIVERSITY": 12,
    "COLLEGE": 12,
    "INSTITUTE": 12,
    "SCHOOL": 10,
    "TRUSTEES": 8,
    "REGENTS": 8,
    "BOARD": 6,
}


def meaningful_tokens(text: str | None) -> list[str]:
    return [token for token in normalize(text).split() if token not in STOPWORDS]


def score_name(candidate_name: str | None, aliases: list[str]) -> int:
    candidate_norm = normalize(candidate_name)
    candidate_tokens = meaningful_tokens(candidate_name)
    best = -999

    for alias in aliases:
        alias_norm = normalize(alias)
        alias_tokens = meaningful_tokens(alias)
        if not alias_tokens:
            continue

        common = len(set(candidate_tokens) & set(alias_tokens))
        score = int(60 * common / len(set(alias_tokens)))
        if common == len(set(alias_tokens)):
            score += 35
        if candidate_norm == alias_norm:
            score += 80
        if candidate_norm.startswith(alias_norm) or alias_norm.startswith(candidate_norm):
            score += 45
        if alias_norm and alias_norm in candidate_norm:
            score += 30

        for word, value in HARD_PENALTIES.items():
            if word in candidate_tokens and word not in alias_tokens:
                score += value
        for word, value in SOFT_PENALTIES.items():
            if word in candidate_tokens and word not in alias_tokens:
                score += value
        for word, value in GOOD_WORDS.items():
            if word in candidate_tokens:
                score += value

        score -= max(0, len(candidate_tokens) - len(alias_tokens)) * 2
        best = max(best, score)

    return best


class Resolver:
    """Caches SAM/ROR/NIH lookups so repeated aliases cost one request each."""

    def __init__(self, source: JsonSource) -> None:
        self.source = source
        self.sam_name_cache: dict[str, list[dict]] = {}
        self.sam_uei_cache: dict[str, dict | None] = {}
        self.ror_cache: dict[str, list[str]] = {}
        self.nih_cache: dict[tuple[str, ...], list[dict]] = {}

    def sam_search_name(self, query: str) -> list[dict]:
        if query in self.sam_name_cache:
            return self.sam_name_cache[query]
        data = self.source.fetch(
            SAM_ENDPOINT,
            params={
                "size": "10",
                "fieldName": "legalBusinessNameSuggest",
                "text": query,
                "index": "entity",
            },
        )
        self.sam_name_cache[query] = data
        time.sleep(0.05)
        return data

    def sam_by_uei(self, uei: str) -> dict | None:
        if uei in self.sam_uei_cache:
            return self.sam_uei_cache[uei]
        data = self.source.fetch(
            SAM_ENDPOINT,
            params={"size": "10", "fieldName": "ueiSamSuggest", "text": uei, "index": "entity"},
        )
        self.sam_uei_cache[uei] = data[0] if data else None
        time.sleep(0.05)
        return self.sam_uei_cache[uei]

    def ror_names(self, ror_id: str | None) -> list[str]:
        if not ror_id:
            return []
        if ror_id in self.ror_cache:
            return self.ror_cache[ror_id]
        data = self.source.fetch(f"{ROR_ENDPOINT}{ror_id}")
        names = []
        for row in data.get("names", []):
            if isinstance(row, dict) and row.get("value"):
                names.append(row["value"])
        resolved = list(dict.fromkeys(name for name in names if name))
        self.ror_cache[ror_id] = resolved
        time.sleep(0.05)
        return resolved

    def nih_orgs(self, aliases: list[str]) -> list[dict]:
        key = tuple(aliases[:8])
        if key in self.nih_cache:
            return self.nih_cache[key]
        payload = {
            "criteria": {"org_names": aliases[:8]},
            "include_fields": ["Organization"],
            "limit": 25,
        }
        data = self.source.fetch(NIH_ENDPOINT, payload=payload)
        by_uei: dict[str, dict] = {}
        for row in data.get("results", []):
            org = row.get("organization") or {}
            uei = org.get("primary_uei") or ""
            if not uei or uei in by_uei:
                continue
            duns_values = org.get("org_duns") or []
            by_uei[uei] = {
                "uei": uei,
                "nih_name": org.get("org_name") or "",
                "duns": org.get("primary_duns") or (duns_values[0] if duns_values else None),
            }
        resolved = list(by_uei.values())
        self.nih_cache[key] = resolved
        time.sleep(0.08)
        return resolved


def resolve_university(
    resolver: Resolver,
    canonical_name: str,
    ror_id: str | None,
) -> tuple[dict | None, list[dict]]:
    aliases = alias_variants(canonical_name)
    aliases.extend(resolver.ror_names(ror_id))
    aliases = list(dict.fromkeys(alias for alias in aliases if alias and len(alias) >= 3))

    candidates: dict[str, dict] = {}

    manual_uei = get_manual_uei()
    if canonical_name in manual_uei:
        uei = manual_uei[canonical_name]
        sam = resolver.sam_by_uei(uei)
        if sam:
            candidates[uei] = {
                "uei": uei,
                "sam_name": sam["legalBusinessName"],
                "duns": None,
                "sources": {"manual_uei"},
            }

    for query in aliases[:12]:
        try:
            for row in resolver.sam_search_name(query):
                uei = row["ueiSAM"]
                entry = candidates.setdefault(
                    uei,
                    {
                        "uei": uei,
                        "sam_name": row["legalBusinessName"],
                        "duns": None,
                        "sources": set(),
                    },
                )
                entry["sources"].add("sam_name")
        except Exception:
            continue

    try:
        for org in resolver.nih_orgs(aliases):
            sam = resolver.sam_by_uei(org["uei"])
            if not sam:
                continue
            entry = candidates.setdefault(
                org["uei"],
                {
                    "uei": org["uei"],
                    "sam_name": sam["legalBusinessName"],
                    "duns": None,
                    "sources": set(),
                },
            )
            entry["sources"].add("nih")
            entry["nih_name"] = org["nih_name"]
            if org.get("duns"):
                entry["duns"] = org["duns"]
        # The NIH side is corroboration only; the SAM lookup above remains the gate.
    except Exception:
        pass

    ranked = []
    for entry in candidates.values():
        score = max(
            score_name(entry.get("sam_name"), aliases),
            score_name(entry.get("nih_name"), aliases),
        )
        if "nih" in entry["sources"]:
            score += 20
        if entry.get("duns"):
            score += 5
        if "manual_uei" in entry["sources"]:
            score += 25
        entry["score"] = score
        ranked.append(entry)

    ranked.sort(key=lambda entry: (-entry["score"], entry.get("sam_name", "")))
    best = ranked[0] if ranked and ranked[0]["score"] >= 85 else None
    return best, ranked[:5]


def load_universities(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    connection.row_factory = sqlite3.Row
    return connection.execute(
        """
        SELECT
            u.university_id,
            u.canonical_name,
            MAX(CASE WHEN ui.system_code='ROR' THEN ui.external_id END) AS ror_id
        FROM universities u
        LEFT JOIN university_identifiers ui
            ON ui.university_id = u.university_id
        GROUP BY u.university_id, u.canonical_name
        ORDER BY u.university_id
        """
    ).fetchall()


def upsert_identifiers(
    connection: sqlite3.Connection, resolved: list[tuple[int, str, dict]]
) -> None:
    import datetime as _dt

    now_iso = _dt.datetime.now(_dt.UTC).isoformat()

    university_ids = [row[0] for row in resolved]
    if university_ids:
        placeholders = ",".join("?" for _ in university_ids)
        connection.execute(
            f"""
            DELETE FROM university_ueis
            WHERE is_primary = 1
              AND university_id IN ({placeholders})
            """,
            university_ids,
        )

    uei_rows = []
    for university_id, _canonical_name, best in resolved:
        uei_rows.append(
            (
                university_id,
                best["uei"],
                best.get("sam_name") or best.get("nih_name"),
                "main",
                1,
                "sam_gov",
                now_iso,
                None,
            )
        )

    connection.executemany(
        """
        INSERT OR REPLACE INTO university_ueis
            (university_id, uei, entity_name, entity_role, is_primary,
             discovery_source, discovered_at, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        uei_rows,
    )


def summarize_unresolved_candidates(
    unresolved_rows: list[tuple[str, list[dict]]],
) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for canonical_name, ranked in unresolved_rows:
        candidates = []
        for entry in ranked[:3]:
            candidates.append(
                {
                    "uei": entry.get("uei"),
                    "sam_name": entry.get("sam_name"),
                    "nih_name": entry.get("nih_name"),
                    "duns": entry.get("duns"),
                    "score": entry.get("score"),
                    "sources": sorted(entry.get("sources", [])),
                }
            )
        details.append({"canonical_name": canonical_name, "candidates": candidates})
    return details


def print_unresolved_candidates(details: list[dict[str, Any]]) -> None:
    """Print human-readable detail also preserved in JSON summary for automation."""
    if not details:
        return
    print("Unresolved UEI candidates:", file=sys.stderr)
    for item in details:
        print(f"  {item['canonical_name']}", file=sys.stderr)
        for candidate in item["candidates"]:
            name = candidate.get("sam_name") or candidate.get("nih_name") or "?"
            print(
                f"    score={candidate.get('score')} uei={candidate.get('uei')} {name}",
                file=sys.stderr,
            )


class AddUeiDunsLoader(ManualDataLoader):
    source_code = "ADD_UEI_DUNS"
    description = "Populate primary UEI/DUNS identifiers via SAM.gov lookup."
    requires_config = False
    requires_json_source = True
    json_source_headers = HEADERS
    default_request_timeout = 30
    execute_help = "write changes to the live SQLite database (default is dry-run)"

    def summary_extras(
        self,
        rows: list[dict[str, Any]],
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        return {"unresolved_candidates": manifest.get("unresolved_candidates", [])}

    def load(
        self,
        *,
        conn: sqlite3.Connection | None,
        config: dict[str, Any],
        args,
        snapshot: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        connection = self._require_conn(conn)
        universities = load_universities(connection)
        resolver = Resolver(self.json_source)

        resolved_rows: list[tuple[int, str, dict]] = []
        unresolved_rows: list[tuple[str, list[dict]]] = []

        total = len(universities)
        for index, row in enumerate(universities, start=1):
            best, ranked = resolve_university(resolver, row["canonical_name"], row["ror_id"])
            status = "resolved" if best else "review"
            print(f"[{index:03d}/{total:03d}] {status}: {row['canonical_name']}", file=sys.stderr)
            if best:
                resolved_rows.append((row["university_id"], row["canonical_name"], best))
            else:
                unresolved_rows.append((row["canonical_name"], ranked))

        if args.execute:
            with connection:
                upsert_identifiers(connection, resolved_rows)

        duns_count = sum(1 for _university_id, _name, best in resolved_rows if best.get("duns"))
        unresolved_candidates = summarize_unresolved_candidates(unresolved_rows)
        print_unresolved_candidates(unresolved_candidates)

        return [], {
            "executed": args.execute,
            "db": str(args.db),
            "resolved": len(resolved_rows),
            "duns": duns_count,
            "unresolved": len(unresolved_rows),
            "unresolved_candidates": unresolved_candidates,
        }


if __name__ == "__main__":
    raise SystemExit(AddUeiDunsLoader().run())
