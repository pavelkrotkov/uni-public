# uni-public

Public-source code for building and exploring undergraduate university research,
outcomes, and field-cluster rankings, with optional local Naviance analysis.

**No real student or family data is distributed.** Naviance records, raw scrapes,
applicant-level scores/GPA/outcomes, generated reports, local databases, and
personal selection notes are deliberately excluded. Test fixtures are invented.
Do not check your own applicant data or generated reports into Git.

## Get started

Python 3.11+ and [uv](https://docs.astral.sh/uv/) are required:

```sh
uv sync --locked --all-extras --dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check .
```

Copy or create your own authorized input data locally. Edit `config.yaml` for
your environment and use credentials from environment variables, never files
committed to this repository. Loaders are dry-run by default; use `--execute`
only when you intend to modify your local SQLite database. Any Naviance imports
require explicit `--high-school-name` and `--high-school-slug` arguments. The
authenticated-site scraper is not part of this release; users must obtain their
own data lawfully and respect third-party access terms and privacy.

The sample feature rows under `tests/` represent fictional universities. No
Naviance history or real applicant scattergram is bundled. This release is a
new, independent snapshot and contains no Git history from the private project.

Code is distributed under the MIT license. External datasets and sources retain
their own licenses and usage restrictions.
