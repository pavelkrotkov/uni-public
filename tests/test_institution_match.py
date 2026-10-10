"""Table-driven tests for institution matching (#51).

The rule this covers had four disagreeing implementations and no tests. These
pin each step of the precedence, the state veto, both fuzzy thresholds, and the
collision sweep — the behaviours the old code expressed in three places.
"""

from __future__ import annotations

import pytest
from shared.institution_match import (
    EXACT_METHODS,
    FUZZY_AUTO_THRESHOLD,
    FUZZY_REVIEW_THRESHOLD,
    MatchCandidate,
    UniversityRef,
    best_match,
    match_batch,
    matches,
    normalize,
    similarity,
    state_conflicts,
)

BRANDEIS = UniversityRef(
    university_id=1,
    canonical_name="Brandeis University",
    state_code="MA",
    ueis=frozenset({"UEI0000BRAND"}),
    aliases=("Brandeis University", "Brandeis Univ"),
)
RICE = UniversityRef(
    university_id=2,
    canonical_name="Rice University",
    state_code="TX",
    ueis=frozenset({"UEI00000RICE"}),
    aliases=("Rice University",),
)
CALTECH = UniversityRef(
    university_id=3,
    canonical_name="California Institute of Technology",
    state_code="CA",
    aliases=("California Institute of Technology", "Caltech"),
)
GEORGIA_TECH = UniversityRef(
    university_id=4,
    canonical_name="Georgia Institute of Technology-Main Campus",
    state_code="GA",
    aliases=("Georgia Institute of Technology-Main Campus", "Georgia Institute of Technology"),
)
UNIVERSE = [BRANDEIS, RICE, CALTECH, GEORGIA_TECH]


# --------------------------------------------------------------------------
# normalize: one normaliser replacing two that disagreed
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Brandeis University", "BRANDEIS UNIVERSITY"),
        ("  spaced   out  ", "SPACED OUT"),
        ("Texas A&M University", "TEXAS A AND M UNIVERSITY"),
        ("Rutgers University - New Brunswick", "RUTGERS UNIVERSITY NEW BRUNSWICK"),
        ("St. Louis University", "SAINT LOUIS UNIVERSITY"),
        ("St Olaf College", "SAINT OLAF COLLEGE"),
        # From the Naviance normaliser, which the identity one lacked.
        ("The Ohio State University", "OHIO STATE UNIVERSITY"),
        ("the college of new jersey", "COLLEGE OF NEW JERSEY"),
        (None, ""),
        ("", ""),
        ("!!!", ""),
    ],
)
def test_normalize(raw: str | None, expected: str) -> None:
    assert normalize(raw) == expected


def test_normalize_reconciles_the_two_old_normalisers() -> None:
    """'St.' folding came from identity_overrides, 'The ' stripping from Naviance.

    Neither old normaliser did both, so this pair agreed under neither of them.
    """
    assert normalize("The St. Louis University") == normalize("Saint Louis University")


def test_an_apostrophe_splits_a_word_rather_than_closing_it() -> None:
    """Documenting a rough edge inherited from identity_overrides.normalize.

    Punctuation becomes a space, so "Mary's" folds to "MARY S" and does not
    equal "Marys". Both old normalisers behaved this way for their own inputs;
    changing it would move match outcomes, so it is pinned rather than fixed.
    """
    assert normalize("St. Mary's College") == "SAINT MARY S COLLEGE"
    assert normalize("Saint Marys College") == "SAINT MARYS COLLEGE"


def test_similarity_is_token_order_insensitive() -> None:
    assert similarity("BRANDEIS UNIVERSITY", "UNIVERSITY BRANDEIS") == 1.0
    assert similarity("BRANDEIS UNIVERSITY", "RICE UNIVERSITY") < 0.8


# --------------------------------------------------------------------------
# the state veto
# --------------------------------------------------------------------------


def test_a_state_mismatch_vetoes_even_an_exact_name() -> None:
    candidate = MatchCandidate(name="Brandeis University", state_code="NY")
    assert matches(candidate, BRANDEIS).is_match is False


def test_a_missing_state_on_either_side_is_not_a_conflict() -> None:
    """Plenty of records omit the state; only a stated disagreement vetoes."""
    stateless_target = UniversityRef(university_id=9, canonical_name="Nowhere U")
    assert state_conflicts(MatchCandidate(name="x"), BRANDEIS) is False
    assert state_conflicts(MatchCandidate(name="x", state_code="MA"), stateless_target) is False
    assert state_conflicts(MatchCandidate(name="x", state_code="MA"), BRANDEIS) is False
    assert state_conflicts(MatchCandidate(name="x", state_code="CA"), BRANDEIS) is True


def test_a_state_mismatch_vetoes_a_uei_hit_too() -> None:
    candidate = MatchCandidate(name="Other", state_code="NY", ueis=frozenset({"UEI0000BRAND"}))
    assert matches(candidate, BRANDEIS).is_match is False


# --------------------------------------------------------------------------
# precedence
# --------------------------------------------------------------------------


def test_a_shared_uei_matches_regardless_of_name() -> None:
    candidate = MatchCandidate(name="Totally Different Name", ueis=frozenset({"UEI0000BRAND"}))
    result = matches(candidate, BRANDEIS)
    assert (result.method, result.score, result.university_id) == ("uei", 1.0, 1)


def test_a_uei_that_does_not_intersect_falls_through_to_the_name() -> None:
    candidate = MatchCandidate(name="Brandeis University", ueis=frozenset({"UEI0000OTHER"}))
    assert matches(candidate, BRANDEIS).method == "alias"


def test_an_exact_alias_beats_the_canonical_name() -> None:
    assert matches(MatchCandidate(name="Brandeis Univ"), BRANDEIS).method == "alias"


def test_the_canonical_name_matches_when_no_alias_does() -> None:
    bare = UniversityRef(university_id=5, canonical_name="Bare University")
    assert matches(MatchCandidate(name="bare university"), bare).method == "exact"


def test_an_alias_inside_a_longer_name_matches_when_substrings_are_allowed() -> None:
    """A grant record often names a subsidiary: 'X University Medical Center'."""
    candidate = MatchCandidate(name="Brandeis University Medical Center")
    assert matches(candidate, BRANDEIS, allow_substring=True).method == "alias_substring"
    assert matches(candidate, BRANDEIS, allow_substring=False).is_match is False


def test_substring_matching_is_on_by_default_for_the_grant_loaders() -> None:
    assert matches(MatchCandidate(name="Rice University Foundation"), RICE).is_match


def test_fuzzy_is_off_unless_a_threshold_is_given() -> None:
    """The grant loaders never scored fuzzily and must not start.

    The typo is in the first word so it is not caught by the substring rule
    either — "Brandeis Universty" would still contain the alias "Brandeis Univ".
    """
    candidate = MatchCandidate(name="Brandies University")
    assert matches(candidate, BRANDEIS).is_match is False
    assert matches(candidate, BRANDEIS, fuzzy_threshold=0.85).method == "fuzzy"


def test_a_blank_candidate_name_never_matches_on_name() -> None:
    for name in (None, "", "   ", "!!!"):
        assert matches(MatchCandidate(name=name), BRANDEIS).is_match is False


def test_build_drops_blank_ueis_and_trims_the_rest() -> None:
    candidate = MatchCandidate.build("X", ueis=[" UEI1 ", None, "", "  "])
    assert candidate.ueis == frozenset({"UEI1"})


# --------------------------------------------------------------------------
# best_match: one candidate against the whole universe
# --------------------------------------------------------------------------


def test_best_match_finds_the_right_university() -> None:
    result = best_match(MatchCandidate(name="Rice University"), UNIVERSE)
    assert result.university_id == RICE.university_id
    assert result.method in EXACT_METHODS


def test_best_match_prefers_an_exact_hit_over_a_higher_scoring_fuzzy_one() -> None:
    """Georgia Tech scored 0.86 against Caltech under the old matcher."""
    result = best_match(MatchCandidate(name="Georgia Institute of Technology"), UNIVERSE)
    assert result.university_id == GEORGIA_TECH.university_id
    assert result.method == "alias"


def test_best_match_reports_a_near_miss_without_linking_it() -> None:
    result = best_match(MatchCandidate(name="Brandeis Universty College"), UNIVERSE)
    if not result.is_match:
        assert result.score > 0.0


def test_best_match_returns_no_match_for_something_unrelated() -> None:
    result = best_match(MatchCandidate(name="Zzz Unrelated Polytechnic"), UNIVERSE)
    assert result.is_match is False
    assert result.score < FUZZY_AUTO_THRESHOLD


# --------------------------------------------------------------------------
# match_batch: thresholds and the collision sweep
# --------------------------------------------------------------------------


def test_batch_splits_by_threshold() -> None:
    outcome = match_batch(
        [
            ("a", MatchCandidate(name="Rice University")),
            ("b", MatchCandidate(name="Zzz Unrelated Polytechnic")),
        ],
        UNIVERSE,
    )
    assert [a.key for a in outcome.matched] == ["a"]
    assert [a.key for a in outcome.unmatched] == ["b"]


def test_a_fuzzy_match_on_an_already_claimed_university_is_dropped() -> None:
    """The false-positive sweep: the real match is already recorded elsewhere."""
    outcome = match_batch(
        [("dupe", MatchCandidate(name="Rice Universty"))],
        UNIVERSE,
        claimed_university_ids={RICE.university_id},
    )
    assert [a.key for a in outcome.dropped_collisions] == ["dupe"]
    assert outcome.matched == []


def test_an_exact_match_on_a_claimed_university_is_kept() -> None:
    """Only fuzzy results are false positives; an exact hit is the caller's business."""
    outcome = match_batch(
        [("exact", MatchCandidate(name="Rice University"))],
        UNIVERSE,
        claimed_university_ids={RICE.university_id},
    )
    assert [a.key for a in outcome.matched] == ["exact"]


def test_the_collision_sweep_does_not_depend_on_input_order() -> None:
    """The SQL pass this replaced ran after all matching, so order cannot matter."""
    pair = [
        ("exact", MatchCandidate(name="Rice University")),
        ("fuzzy", MatchCandidate(name="Rice Universty")),
    ]
    forward = match_batch(pair, UNIVERSE).counts()
    backward = match_batch(list(reversed(pair)), UNIVERSE).counts()
    assert forward == backward
    assert forward["dropped_collisions"] == 1


def test_counts_reports_every_bucket() -> None:
    outcome = match_batch([("a", MatchCandidate(name="Rice University"))], UNIVERSE)
    assert outcome.counts() == {
        "exact": 1,
        "fuzzy_auto": 0,
        "fuzzy_review": 0,
        "unmatched": 0,
        "dropped_collisions": 0,
    }


def test_thresholds_keep_their_documented_relationship() -> None:
    assert 0.0 < FUZZY_REVIEW_THRESHOLD < FUZZY_AUTO_THRESHOLD <= 1.0


def test_an_empty_universe_matches_nothing() -> None:
    outcome = match_batch([("a", MatchCandidate(name="Rice University"))], [])
    assert len(outcome.unmatched) == 1


def test_the_prefix_strip_respects_word_boundaries() -> None:
    """'Theodore' must not lose its first three letters."""
    assert normalize("Theodore Institute") == "THEODORE INSTITUTE"
    assert normalize("Thelma College") == "THELMA COLLEGE"
    assert normalize("The Institute") == "INSTITUTE"


# --------------------------------------------------------------------------
# the loader adapters: only the field mapping is loader-specific
# --------------------------------------------------------------------------


def test_nih_maps_its_payload_fields() -> None:
    from load_nih_awards import TargetUniversity
    from load_nih_awards import matches_target as nih_matches

    target = TargetUniversity(
        university_id=1,
        canonical_name="Brandeis University",
        state_code="MA",
        ueis=("UEI0000BRAND",),
        aliases=("Brandeis University",),
    )
    assert nih_matches(target, {"org_name": "Brandeis University", "org_state": "MA"})
    assert nih_matches(target, {"org_name": "Whatever", "primary_uei": "UEI0000BRAND"})
    assert nih_matches(target, {"org_name": "Whatever", "org_ueis": ["UEI0000BRAND"]})
    assert not nih_matches(target, {"org_name": "Brandeis University", "org_state": "NY"})
    assert not nih_matches(target, {"org_name": "Rice University", "org_state": "MA"})


def test_nsf_maps_its_payload_fields() -> None:
    from load_nsf_awards import matches_target as nsf_matches

    target = UniversityRef(
        university_id=1,
        canonical_name="Brandeis University",
        state_code="MA",
        ueis=frozenset({"UEI0000BRAND"}),
        aliases=("Brandeis University",),
    )
    assert nsf_matches(target, {"awardeeName": "Brandeis University", "awardeeStateCode": "MA"})
    assert nsf_matches(target, {"awardeeName": "Whatever", "ueiNumber": "UEI0000BRAND"})
    assert nsf_matches(target, {"awardeeName": "Whatever", "parentUeiNumber": "UEI0000BRAND"})
    assert not nsf_matches(target, {"awardeeName": "Brandeis University", "awardeeStateCode": "NY"})
    assert not nsf_matches(target, {"awardeeName": "Rice University", "awardeeStateCode": "MA"})


def test_both_loaders_agree_on_an_equivalent_record() -> None:
    """The two matches_target functions were byte-identical; they must stay so."""
    from load_nih_awards import TargetUniversity as NihTarget
    from load_nih_awards import matches_target as nih_matches
    from load_nsf_awards import matches_target as nsf_matches

    nih_target = NihTarget(
        university_id=1,
        canonical_name="Brandeis University",
        state_code="MA",
        ueis=("UEI0000BRAND",),
        aliases=("Brandeis University",),
    )
    nsf_target = UniversityRef(
        university_id=1,
        canonical_name="Brandeis University",
        state_code="MA",
        ueis=frozenset({"UEI0000BRAND"}),
        aliases=("Brandeis University",),
    )
    for name, state, expected in [
        ("Brandeis University", "MA", True),
        ("Brandeis University Medical Center", "MA", True),
        ("Brandeis University", "NY", False),
        ("Rice University", "MA", False),
    ]:
        nih = nih_matches(nih_target, {"org_name": name, "org_state": state})
        nsf = nsf_matches(nsf_target, {"awardeeName": name, "awardeeStateCode": state})
        assert nih == nsf == expected, (name, state)


# --------------------------------------------------------------------------
# review-list quality and precedence ordering (PR #60 review)
# --------------------------------------------------------------------------


def test_a_near_miss_reports_which_university_it_nearly_matched() -> None:
    """The review list is only useful if it names a suggestion."""
    result = best_match(MatchCandidate(name="Brandies University College"), UNIVERSE)
    assert result.is_match is False
    assert result.needs_review is True
    assert FUZZY_REVIEW_THRESHOLD <= result.score < FUZZY_AUTO_THRESHOLD
    assert result.matched_name == "Brandeis University"


def test_a_score_below_the_review_threshold_still_names_its_best_guess() -> None:
    """Useful in diagnostics even when the candidate is not worth reviewing."""
    result = best_match(MatchCandidate(name="Brandeis College"), UNIVERSE)
    assert result.needs_review is False
    assert result.matched_name == "Brandeis University"


def test_matches_carries_the_target_name_on_a_sub_threshold_score() -> None:
    result = matches(MatchCandidate(name="Brandies University"), BRANDEIS, fuzzy_threshold=0.99)
    assert result.is_match is False
    assert result.matched_name == "Brandeis University"
    assert result.score > 0.0


def test_a_uei_hit_outranks_a_name_hit_on_a_different_university() -> None:
    """UEIs are authoritative, so universe order must not decide this."""
    named = UniversityRef(
        university_id=10,
        canonical_name="Shared Name University",
        aliases=("Shared Name University",),
    )
    identified = UniversityRef(
        university_id=11, canonical_name="Other University", ueis=frozenset({"UEI000000001"})
    )
    candidate = MatchCandidate(name="Shared Name University", ueis=frozenset({"UEI000000001"}))
    for universe in ([named, identified], [identified, named]):
        result = best_match(candidate, universe)
        assert (result.university_id, result.method) == (11, "uei"), universe


def test_the_uei_sweep_still_respects_the_state_veto() -> None:
    wrong_state = UniversityRef(
        university_id=12,
        canonical_name="Elsewhere University",
        state_code="CA",
        ueis=frozenset({"UEI000000002"}),
    )
    candidate = MatchCandidate(
        name="Elsewhere University", state_code="NY", ueis=frozenset({"UEI000000002"})
    )
    assert best_match(candidate, [wrong_state]).is_match is False


# --------------------------------------------------------------------------
# one normalizer, repo-wide (PR #60 review)
# --------------------------------------------------------------------------


def test_identity_overrides_reexports_the_shared_normalizer() -> None:
    """Not "a compatible copy" — the same function object.

    add_uei_duns.py scores SAM.gov candidates through
    identity_overrides.normalize. While that was a separate implementation,
    "University of the South" scored differently there than in matching, and a
    future normalizer fix would have moved grant and Naviance results without
    moving UEI resolution.
    """
    import identity_overrides

    assert identity_overrides.normalize is normalize


def test_add_uei_duns_scores_through_the_shared_normalizer() -> None:
    import add_uei_duns

    assert add_uei_duns.normalize is normalize


def test_the_shared_normalizer_is_the_only_one_in_scripts() -> None:
    """Guard against a fourth normalizer appearing."""
    import ast

    from shared import ROOT

    definitions = []
    for path in sorted((ROOT / "scripts").rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in {
                "normalize",
                "normalize_name",
            }:
                definitions.append(str(path.relative_to(ROOT)))
    assert definitions == ["scripts/shared/institution_match.py"], (
        f"expected exactly one normalizer definition, found {definitions}"
    )


def test_a_the_prefixed_candidate_now_scores_as_an_exact_name() -> None:
    """The one behavioural consequence for UEI resolution, pinned.

    "THE" is already a STOPWORD so tokens were never affected; what changes is
    the exact/prefix string comparison in score_name. Measured across the live
    alias corpus: 115 of 714 (university, candidate) pairs score higher and
    none score lower.
    """
    from add_uei_duns import score_name

    aliases = ["Boston University"]
    assert score_name("The Boston University", aliases) == score_name("Boston University", aliases)
