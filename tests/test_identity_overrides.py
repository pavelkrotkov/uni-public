from __future__ import annotations

import json
from pathlib import Path

import identity_overrides
import pytest
from identity_overrides import (
    IdentityOverridesError,
    alias_variants,
    get_identity_overrides,
    get_manual_aliases,
    get_manual_uei,
    load_identity_overrides,
    normalize,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, ""),
        ("", ""),
        ("St. John's", "SAINT JOHN S"),
        ("A&M", "A AND M"),
        ("Texas A & M", "TEXAS A AND M"),
        ("  Foo--Bar  ", "FOO BAR"),
        ("Stevens", "STEVENS"),
        ("123-ABC!", "123 ABC"),
    ],
)
def test_normalize(raw: str | None, expected: str) -> None:
    assert normalize(raw) == expected


def _write_overrides(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "identity_overrides.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_validate_aliases_happy_path(tmp_path: Path) -> None:
    path = _write_overrides(
        tmp_path,
        {
            "manual_aliases": {
                "Foo University": ["Foo U"],
                "Bar College": ["Bar C", "Barr College"],
            },
            "manual_uei": {},
        },
    )
    aliases, manual_uei = load_identity_overrides(path)
    assert aliases == {
        "Foo University": ["Foo U"],
        "Bar College": ["Bar C", "Barr College"],
    }
    assert manual_uei == {}


def test_load_identity_overrides_non_object_payload_raises(tmp_path: Path) -> None:
    path = _write_overrides(tmp_path, [1, 2, 3])
    with pytest.raises(IdentityOverridesError) as excinfo:
        load_identity_overrides(path)
    assert str(path) in str(excinfo.value)


def test_validate_aliases_non_dict_payload_raises(tmp_path: Path) -> None:
    path = _write_overrides(tmp_path, {"manual_aliases": ["not", "a", "dict"]})
    with pytest.raises(IdentityOverridesError) as excinfo:
        load_identity_overrides(path)
    assert str(path) in str(excinfo.value)
    assert "manual_aliases" in str(excinfo.value)


def test_validate_aliases_non_list_values_raises(tmp_path: Path) -> None:
    path = _write_overrides(
        tmp_path,
        {"manual_aliases": {"Foo": "not a list"}},
    )
    with pytest.raises(IdentityOverridesError) as excinfo:
        load_identity_overrides(path)
    assert "must map strings to lists" in str(excinfo.value)


def test_validate_aliases_non_string_alias_entry_raises(tmp_path: Path) -> None:
    path = _write_overrides(
        tmp_path,
        {"manual_aliases": {"Foo": ["ok", 42]}},
    )
    with pytest.raises(IdentityOverridesError) as excinfo:
        load_identity_overrides(path)
    assert "Foo" in str(excinfo.value)


def test_validate_aliases_duplicate_alias_across_keys_via_normalization(tmp_path: Path) -> None:
    path = _write_overrides(
        tmp_path,
        {
            "manual_aliases": {
                "School A": ["Foo & Bar"],
                "School B": ["Foo and Bar"],
            }
        },
    )
    with pytest.raises(IdentityOverridesError) as excinfo:
        load_identity_overrides(path)
    message = str(excinfo.value)
    assert "School A" in message
    assert "School B" in message


def test_validate_aliases_same_alias_twice_under_same_key_allowed(tmp_path: Path) -> None:
    path = _write_overrides(
        tmp_path,
        {"manual_aliases": {"Foo": ["Bar", "Bar"]}},
    )
    aliases, _ = load_identity_overrides(path)
    assert aliases == {"Foo": ["Bar", "Bar"]}


def test_validate_aliases_empty_alias_skipped_from_dup_check_but_kept_in_output(
    tmp_path: Path,
) -> None:
    # Empty / whitespace-only aliases normalize to "" and are silently skipped
    # by the cross-key duplicate detector — but the original `values` list is
    # still assigned to the output unchanged.
    path = _write_overrides(
        tmp_path,
        {
            "manual_aliases": {
                "Foo": ["", "   "],
                "Bar": ["", "   "],
            }
        },
    )
    aliases, _ = load_identity_overrides(path)
    assert aliases == {"Foo": ["", "   "], "Bar": ["", "   "]}


def test_validate_manual_uei_happy_path_normalizes(tmp_path: Path) -> None:
    path = _write_overrides(
        tmp_path,
        {"manual_uei": {"Foo University": "  abcdef123456  "}},
    )
    _, manual_uei = load_identity_overrides(path)
    assert manual_uei == {"Foo University": "ABCDEF123456"}


def test_validate_manual_uei_invalid_value_raises(tmp_path: Path) -> None:
    path = _write_overrides(
        tmp_path,
        {"manual_uei": {"Foo University": "too-short"}},
    )
    with pytest.raises(IdentityOverridesError) as excinfo:
        load_identity_overrides(path)
    message = str(excinfo.value)
    assert "Foo University" in message
    assert "too-short" in message


def test_validate_manual_uei_non_string_value_raises(tmp_path: Path) -> None:
    path = _write_overrides(
        tmp_path,
        {"manual_uei": {"Foo University": 123456789012}},
    )
    with pytest.raises(IdentityOverridesError) as excinfo:
        load_identity_overrides(path)
    assert "must map strings to strings" in str(excinfo.value)


def test_load_identity_overrides_missing_file_raises(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.json"
    with pytest.raises(IdentityOverridesError) as excinfo:
        load_identity_overrides(missing)
    assert str(missing) in str(excinfo.value)


def test_load_identity_overrides_invalid_json_raises(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{ not valid json ", encoding="utf-8")
    with pytest.raises(IdentityOverridesError) as excinfo:
        load_identity_overrides(path)
    assert str(path) in str(excinfo.value)


def test_load_identity_overrides_honors_custom_path(tmp_path: Path) -> None:
    path = _write_overrides(
        tmp_path,
        {"manual_aliases": {"Foo": ["Bar"]}, "manual_uei": {}},
    )
    aliases, manual_uei = load_identity_overrides(path)
    assert aliases == {"Foo": ["Bar"]}
    assert manual_uei == {}


def test_load_identity_overrides_missing_keys_default_to_empty(tmp_path: Path) -> None:
    path = _write_overrides(tmp_path, {})
    aliases, manual_uei = load_identity_overrides(path)
    assert aliases == {}
    assert manual_uei == {}


def test_alias_variants_university_of(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(identity_overrides, "get_manual_aliases", lambda: {})
    variants = alias_variants("University of Chicago")
    assert "University of Chicago" in variants
    assert "Chicago University" in variants
    assert "Chicago, University of" in variants


def test_alias_variants_college_of(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(identity_overrides, "get_manual_aliases", lambda: {})
    variants = alias_variants("College of William")
    assert "College of William" in variants
    assert "The College of William" in variants
    assert "College of William, The" in variants


def test_alias_variants_ampersand_swap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(identity_overrides, "get_manual_aliases", lambda: {})
    variants = alias_variants("Texas A&M")
    assert "Texas AandM" in variants


def test_alias_variants_and_to_ampersand(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(identity_overrides, "get_manual_aliases", lambda: {})
    variants = alias_variants("Foo and Bar")
    assert "Foo & Bar" in variants


def test_alias_variants_appends_manual_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        identity_overrides,
        "get_manual_aliases",
        lambda: {"Foo University": ["Foo Manual Alias"]},
    )
    variants = alias_variants("Foo University")
    assert "Foo Manual Alias" in variants


def test_alias_variants_dedupes_and_preserves_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        identity_overrides,
        "get_manual_aliases",
        lambda: {"University of Chicago": ["University of Chicago", "Chicago University"]},
    )
    variants = alias_variants("University of Chicago")
    assert variants == [
        "University of Chicago",
        "Chicago University",
        "Chicago, University of",
    ]


def test_identity_overrides_error_is_value_error() -> None:
    assert issubclass(IdentityOverridesError, ValueError)


def test_manual_accessors_return_halves_of_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        identity_overrides,
        "load_identity_overrides",
        lambda: ({"Foo": ["Bar"]}, {"Foo": "ABCDEF123456"}),
    )
    get_identity_overrides.cache_clear()
    assert get_manual_aliases() == {"Foo": ["Bar"]}
    assert get_manual_uei() == {"Foo": "ABCDEF123456"}


def test_get_identity_overrides_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    call_count = {"n": 0}

    def fake_load() -> tuple[dict[str, list[str]], dict[str, str]]:
        call_count["n"] += 1
        return ({}, {})

    monkeypatch.setattr(identity_overrides, "load_identity_overrides", fake_load)
    get_identity_overrides.cache_clear()
    first = get_identity_overrides()
    second = get_identity_overrides()
    assert first is second
    assert call_count["n"] == 1


def test_main_smoke_prints_summary_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        identity_overrides,
        "load_identity_overrides",
        lambda: ({"Foo": ["Bar"]}, {"Foo": "ABCDEF123456"}),
    )
    monkeypatch.setattr("sys.argv", ["identity_overrides.py"])
    exit_code = identity_overrides.IdentityOverridesLoader().run()
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["manual_aliases"] == 1
    assert payload["manual_uei"] == 1
    assert exit_code == 0
