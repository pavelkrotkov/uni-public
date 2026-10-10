"""Deciding whether an external record refers to one of our universities.

This is the central domain question in the repo, and it used to have four
implementations that disagreed:

* ``load_nih_awards.matches_target`` and ``load_nsf_awards.matches_target`` —
  byte-identical logic differing only in the field names they read.
* ``identity_overrides.normalize`` — uppercases, ``&``→``AND``, ``-``→space,
  ``ST``→``SAINT``.
* ``load_naviance.normalize_name`` — lowercases and strips a leading ``"the "``,
  so "St. Louis" normalised two different ways depending on which loader ran,
  and Naviance could not use the curated aliases in
  ``data/identity_overrides.json`` at all.
* ``load_naviance.run_fuzzy_match`` — thresholds, scoring, a SQL collision
  sweep, identifier inserts and stderr reporting in one function.

One module now owns it. Callers supply a :class:`MatchCandidate` built from
their own payload shape; nothing else about their payloads is known here.

Two directions, because the callers ask from opposite ends:

* :func:`matches` — "does this record refer to *this* university?" The grant
  loaders iterate their own targets and need a yes/no per record.
* :func:`best_match` and :func:`match_batch` — "which university is this?"
  Naviance has a name and needs to find the university.

Precedence is the same either way: UEI intersection, then an exact alias, then
an exact normalised name, then a fuzzy score above the auto threshold. A state
mismatch vetoes everything, because two schools with similar names in different
states are usually different schools.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

#: Auto-link at or above this fuzzy score.
FUZZY_AUTO_THRESHOLD = 0.85

#: Between this and the auto threshold, report for human review but do not link.
FUZZY_REVIEW_THRESHOLD = 0.70

MatchMethod = Literal["uei", "alias", "exact", "alias_substring", "fuzzy"]

#: Methods that represent a confident, non-fuzzy identification.
EXACT_METHODS: frozenset[str] = frozenset({"uei", "alias", "exact"})

_LEADING_THE = re.compile(r"^THE ")
_NON_ALNUM = re.compile(r"[^A-Z0-9 ]+")
_SAINT = re.compile(r"\bST\b")
_WHITESPACE = re.compile(r"\s+")


def normalize(text: str | None) -> str:
    """Fold a university name to its comparable form.

    Reconciles the two normalisers that used to exist. The
    ``identity_overrides`` version was the more considered one and is the base;
    the leading-``"the "`` strip is carried over from the Naviance version so
    "The Ohio State University" and "Ohio State University" still agree.
    """
    text = (text or "").upper().replace("&", " AND ").replace("-", " ")
    text = _NON_ALNUM.sub(" ", text)
    text = _SAINT.sub("SAINT", text)
    text = _WHITESPACE.sub(" ", text).strip()
    return _LEADING_THE.sub("", text)


def similarity(left: str, right: str) -> float:
    """Token-order-insensitive similarity of two already-normalised names, 0 to 1.

    rapidfuzz is imported lazily so callers that never score fuzzily do not need
    it declared. The grant loaders are in that position: they pass no
    ``fuzzy_threshold``, so they match on UEIs and names alone.
    """
    from rapidfuzz import fuzz

    return fuzz.token_sort_ratio(left, right) / 100.0


@dataclass(frozen=True)
class MatchCandidate:
    """An external record that might refer to one of our universities.

    Each loader maps its own payload into this: NIH reads ``org_name`` /
    ``org_state`` / ``primary_uei``, NSF reads ``awardeeName`` /
    ``awardeeStateCode`` / ``ueiNumber``, Naviance has only a name.
    """

    name: str | None
    state_code: str | None = None
    ueis: frozenset[str] = frozenset()

    @classmethod
    def build(
        cls,
        name: str | None,
        *,
        state_code: str | None = None,
        ueis: Iterable[str | None] = (),
    ) -> MatchCandidate:
        """Construct one, dropping blank UEIs and trimming the rest."""
        cleaned = frozenset(value.strip() for value in ueis if value and value.strip())
        return cls(name=name, state_code=state_code, ueis=cleaned)


@dataclass(frozen=True)
class UniversityRef:
    """One of our universities, as much of it as matching needs."""

    university_id: int
    canonical_name: str
    state_code: str | None = None
    ueis: frozenset[str] = frozenset()
    aliases: tuple[str, ...] = ()

    @property
    def normalized_name(self) -> str:
        return normalize(self.canonical_name)

    @property
    def normalized_aliases(self) -> frozenset[str]:
        names = {normalize(alias) for alias in self.aliases if alias}
        names.discard("")
        return frozenset(names)


@dataclass(frozen=True)
class MatchResult:
    """The outcome of matching one candidate.

    ``university_id`` is ``None`` when nothing matched confidently. ``score``
    is still populated for a near miss so the caller can report it for review.
    """

    university_id: int | None
    method: MatchMethod | None
    score: float
    matched_name: str | None = None

    @property
    def is_match(self) -> bool:
        return self.university_id is not None

    @property
    def needs_review(self) -> bool:
        """A near miss: scored well enough to be worth a human look, not linked."""
        return not self.is_match and self.score >= FUZZY_REVIEW_THRESHOLD


NO_MATCH = MatchResult(university_id=None, method=None, score=0.0)


def state_conflicts(candidate: MatchCandidate, target: UniversityRef) -> bool:
    """True when both sides state a location and they disagree.

    A missing state on either side is not a conflict — plenty of records omit it.
    """
    return bool(
        candidate.state_code and target.state_code and candidate.state_code != target.state_code
    )


def matches(
    candidate: MatchCandidate,
    target: UniversityRef,
    *,
    allow_substring: bool = True,
    fuzzy_threshold: float | None = None,
) -> MatchResult:
    """Does ``candidate`` refer to ``target``?

    ``allow_substring`` keeps the grant loaders' behaviour, where an alias
    appearing anywhere inside the record's name counts — "Brandeis University"
    matching "Brandeis University Medical Center". It is off for callers that
    want a tighter test.

    ``fuzzy_threshold`` enables fuzzy scoring; the grant loaders leave it unset
    because they never scored fuzzily.
    """
    if state_conflicts(candidate, target):
        return NO_MATCH

    if candidate.ueis and target.ueis and (candidate.ueis & target.ueis):
        return MatchResult(target.university_id, "uei", 1.0, target.canonical_name)

    if not candidate.name:
        return NO_MATCH

    candidate_norm = normalize(candidate.name)
    if not candidate_norm:
        return NO_MATCH

    alias_norms = target.normalized_aliases
    if candidate_norm in alias_norms:
        return MatchResult(target.university_id, "alias", 1.0, target.canonical_name)
    if candidate_norm == target.normalized_name:
        return MatchResult(target.university_id, "exact", 1.0, target.canonical_name)

    if allow_substring and any(alias in candidate_norm for alias in alias_norms):
        return MatchResult(target.university_id, "alias_substring", 1.0, target.canonical_name)

    if fuzzy_threshold is not None:
        score = max(
            (similarity(candidate_norm, alias) for alias in alias_norms),
            default=0.0,
        )
        score = max(score, similarity(candidate_norm, target.normalized_name))
        if score >= fuzzy_threshold:
            return MatchResult(target.university_id, "fuzzy", score, target.canonical_name)
        # Carry the name even though this is not a match: the review list is
        # only useful if it says which university the candidate nearly hit.
        return MatchResult(None, None, score, target.canonical_name)

    return NO_MATCH


def best_match(
    candidate: MatchCandidate,
    universe: Sequence[UniversityRef],
    *,
    allow_substring: bool = False,
    fuzzy_threshold: float = FUZZY_AUTO_THRESHOLD,
) -> MatchResult:
    """Which university in ``universe`` does ``candidate`` refer to?

    Exact identifications win outright and stop the search. Otherwise the
    highest fuzzy score is returned — linked if it clears ``fuzzy_threshold``,
    and otherwise carried on the result so the caller can decide whether it is
    worth review.
    """
    # UEIs are authoritative, so they are swept across the whole universe before
    # any name is considered. Returning on the first name hit would otherwise
    # let universe order decide between a name match on one university and a
    # UEI match on another.
    if candidate.ueis:
        for target in universe:
            if state_conflicts(candidate, target):
                continue
            if candidate.ueis & target.ueis:
                return MatchResult(target.university_id, "uei", 1.0, target.canonical_name)

    best = NO_MATCH
    for target in universe:
        result = matches(
            candidate,
            target,
            allow_substring=allow_substring,
            fuzzy_threshold=fuzzy_threshold,
        )
        if result.is_match and result.method in EXACT_METHODS:
            return result
        if result.score > best.score:
            best = result
    return best


@dataclass(frozen=True)
class BatchAssignment:
    """One candidate's resolved match, tagged with the caller's own key."""

    key: Any
    candidate: MatchCandidate
    result: MatchResult


@dataclass
class BatchOutcome:
    """What :func:`match_batch` decided, split by what the caller should do."""

    matched: list[BatchAssignment] = field(default_factory=list)
    review: list[BatchAssignment] = field(default_factory=list)
    unmatched: list[BatchAssignment] = field(default_factory=list)
    dropped_collisions: list[BatchAssignment] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        """Per-method counts plus the totals, for a run summary."""
        summary: dict[str, int] = {
            "exact": 0,
            "fuzzy_auto": 0,
            "fuzzy_review": len(self.review),
            "unmatched": len(self.unmatched),
            "dropped_collisions": len(self.dropped_collisions),
        }
        for assignment in self.matched:
            if assignment.result.method == "fuzzy":
                summary["fuzzy_auto"] += 1
            else:
                summary["exact"] += 1
        return summary


def match_batch(
    candidates: Sequence[tuple[Any, MatchCandidate]],
    universe: Sequence[UniversityRef],
    *,
    claimed_university_ids: Iterable[int] = (),
    allow_substring: bool = False,
    fuzzy_threshold: float = FUZZY_AUTO_THRESHOLD,
    review_threshold: float = FUZZY_REVIEW_THRESHOLD,
) -> BatchOutcome:
    """Resolve many candidates at once, dropping fuzzy collisions.

    ``claimed_university_ids`` are universities already linked by an exact or
    manual match elsewhere. A *fuzzy* result landing on one of those is dropped:
    they are false positives where a different school scores highly on shared
    tokens, and the real match is already recorded. Exact results are never
    dropped, since two candidates legitimately resolving to one university is
    the caller's business, not a scoring artefact.
    """
    outcome = BatchOutcome()

    # Resolve everything first. The collision sweep must see the final set of
    # exact claims, not a partial one — the SQL pass this replaces ran after all
    # matching, so making it order-dependent here would change results with the
    # input order.
    assignments = [
        BatchAssignment(
            key=key,
            candidate=candidate,
            result=best_match(
                candidate,
                universe,
                allow_substring=allow_substring,
                fuzzy_threshold=fuzzy_threshold,
            ),
        )
        for key, candidate in candidates
    ]

    claimed = set(claimed_university_ids)
    for assignment in assignments:
        result = assignment.result
        if result.university_id is not None and result.method in EXACT_METHODS:
            claimed.add(result.university_id)

    for assignment in assignments:
        result = assignment.result
        if result.is_match:
            if result.method == "fuzzy" and result.university_id in claimed:
                outcome.dropped_collisions.append(assignment)
            else:
                outcome.matched.append(assignment)
        elif result.score >= review_threshold:
            outcome.review.append(assignment)
        else:
            outcome.unmatched.append(assignment)

    return outcome
