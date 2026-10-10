# Contributing

This is a small internal Python script repo managed with `uv`.

## Setup

```sh
uv sync --all-extras --dev
uv run pre-commit install
```

## Local checks

```sh
uv run pytest
uv run ruff check .
uv run ruff format .
uv run ty check .
```

`pytest` runs with branch coverage over `scripts/` and reports missing lines. Coverage is
informational only; this repo does not enforce a fail-under threshold.

## GitHub feature gates

CodeQL and Dependency Review are skipped for private repositories unless the matching repository
variables are enabled:

- `ENABLE_CODEQL=true`
- `ENABLE_DEPENDENCY_REVIEW=true`

Only enable those gates after the corresponding GitHub security features are available for the repo.
