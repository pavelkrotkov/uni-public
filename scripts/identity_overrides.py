#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML>=6.0"]
# ///

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from shared import ROOT
from shared.data_loader import ManualDataLoader
from shared.institution_match import normalize as _normalize

DEFAULT_IDENTITY_OVERRIDES_PATH = ROOT / "data" / "identity_overrides.json"
UEI_PATTERN = re.compile(r"[A-Z0-9]{12}")


class IdentityOverridesError(ValueError):
    """Raised when identity override data is missing or invalid."""


# Re-exported so there is exactly one normalizer in the repo. It lives in
# shared.institution_match because that is where matching lives; importing it
# here keeps the historical `from identity_overrides import normalize` working
# for callers such as add_uei_duns.py.
normalize = _normalize


def _validate_aliases(raw_aliases: Any) -> dict[str, list[str]]:
    if not isinstance(raw_aliases, dict):
        raise IdentityOverridesError("identity overrides manual_aliases must be an object")

    aliases: dict[str, list[str]] = {}
    seen_aliases: dict[str, str] = {}
    for name, values in raw_aliases.items():
        if not isinstance(name, str) or not isinstance(values, list):
            raise IdentityOverridesError(
                "identity overrides manual_aliases entries must map strings to lists"
            )
        if not all(isinstance(value, str) for value in values):
            raise IdentityOverridesError(f"identity overrides aliases for {name!r} must be strings")
        for value in values:
            normalized_alias = normalize(value)
            if not normalized_alias:
                continue
            previous_name = seen_aliases.get(normalized_alias)
            if previous_name and previous_name != name:
                raise IdentityOverridesError(
                    f"identity override alias {value!r} is assigned to both "
                    f"{previous_name!r} and {name!r}"
                )
            seen_aliases[normalized_alias] = name
        aliases[name] = values
    return aliases


def _validate_manual_uei(raw_manual_uei: Any) -> dict[str, str]:
    if not isinstance(raw_manual_uei, dict):
        raise IdentityOverridesError("identity overrides manual_uei must be an object")

    manual_uei: dict[str, str] = {}
    for name, uei in raw_manual_uei.items():
        if not isinstance(name, str) or not isinstance(uei, str):
            raise IdentityOverridesError(
                "identity overrides manual_uei entries must map strings to strings"
            )
        normalized_uei = uei.strip().upper()
        if not UEI_PATTERN.fullmatch(normalized_uei):
            raise IdentityOverridesError(
                f"identity overrides manual_uei entry {name!r} has invalid UEI {uei!r}"
            )
        manual_uei[name] = normalized_uei
    return manual_uei


def load_identity_overrides(
    path: Path = DEFAULT_IDENTITY_OVERRIDES_PATH,
) -> tuple[dict[str, list[str]], dict[str, str]]:
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise IdentityOverridesError(
            f"Failed to read identity overrides from {path}: {exc}. "
            f"Expected a UTF-8 JSON file at {path}."
        ) from exc

    if not isinstance(payload, dict):
        raise IdentityOverridesError(
            f"Invalid identity overrides in {path}: file must contain a JSON object"
        )

    try:
        return (
            _validate_aliases(payload.get("manual_aliases", {})),
            _validate_manual_uei(payload.get("manual_uei", {})),
        )
    except IdentityOverridesError as exc:
        raise IdentityOverridesError(f"Invalid identity overrides in {path}: {exc}") from exc


@lru_cache(maxsize=1)
def get_identity_overrides() -> tuple[dict[str, list[str]], dict[str, str]]:
    return load_identity_overrides()


def get_manual_aliases() -> dict[str, list[str]]:
    return get_identity_overrides()[0]


def get_manual_uei() -> dict[str, str]:
    return get_identity_overrides()[1]


def alias_variants(name: str) -> list[str]:
    aliases = [name]
    if name.startswith("University of "):
        tail = name[len("University of ") :]
        aliases.extend([f"{tail} University", f"{tail}, University of"])
    if name.startswith("College of "):
        tail = name[len("College of ") :]
        aliases.extend([f"The College of {tail}", f"College of {tail}, The"])
    if "&" in name:
        aliases.append(name.replace("&", "and"))
    if " and " in name.lower():
        aliases.append(re.sub(r"(?i) and ", " & ", name))
    aliases.extend(get_manual_aliases().get(name, []))
    return list(dict.fromkeys(alias for alias in aliases if alias))


class IdentityOverridesLoader(ManualDataLoader):
    source_code = "IDENTITY_OVERRIDES"
    description = "Validate and summarize identity override data."
    requires_db = False
    requires_config = False

    def load(
        self,
        *,
        conn,
        config: dict[str, Any],
        args,
        snapshot: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        aliases, manual_uei = load_identity_overrides()
        return [], {
            "executed": args.execute,
            "manual_aliases": len(aliases),
            "manual_uei": len(manual_uei),
        }


if __name__ == "__main__":
    raise SystemExit(IdentityOverridesLoader().run())
