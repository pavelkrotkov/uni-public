# Undergraduate College Ranking by Field Cluster — Complete Roadmap

Primary specification for a coding assistant building the pipeline.
Paired with `AGENTS.md`, which is the shorter execution quick-reference for this repo.

---

## 1. Objective

Build a reproducible, config-driven pipeline that ranks U.S. colleges for undergraduate study in user-defined **field clusters** (e.g., Molecular Biology, Genetics, Statistics).

The ranking universe in this repo is intentionally limited to the curated seeded institution set loaded into `universities`. The pipeline does **not** attempt to discover or rank the full national IPEDS universe. IPEDS is used to enrich, validate, and backfill the seeded universe where applicable.

The analytical unit is `university_id × field_cluster_slug`.

For every such pair the pipeline must answer:

1. Does the university offer and sustain relevant bachelor's programs?
2. How strong is its research environment in the corresponding scholarly areas?
3. What are the undergraduate-facing outcomes — field-specific where available, institution-level as fallback?
4. How affordable and accessible is the institution?
5. How much confidence should we place in the result given data gaps and mapping ambiguity?

The system produces **field-specific rankings**, not a single global ranking.

### 1.1 Non-goals

The pipeline does not attempt to measure from public data alone:

- lab culture or advising quality
- undergraduate research access guarantees
- class size within the target major
- mentorship quality

These may be added later as optional enrichment layers.

---

## 2. Execution rules

These rules govern every coding task in this repo.

### 2.1 Smallest correct change

- Make targeted changes only. Do not rebuild the database for a small request.
- For one-off updates, prefer direct SQL or a self-contained `uv` script over rerunning `scripts/bootstrap_db.py`.

### 2.2 Schema is closed by default

- The persisted schema defined in §7 is the authority. Do not add tables or columns unless the user explicitly asks.
- If a deliverable can be produced as a view, Parquet, CSV, JSON, or YAML, prefer that over a schema change.

### 2.3 Inspect before writing

- Before changing data, inspect the live state of affected tables.
- Verify row counts, keys, and nulls. Avoid broad "recovery" actions unless justified.

### 2.4 Self-contained scripts

- New helper scripts must run with `uv` and declare their dependencies in-file.
- Keep scripts narrow and easy to delete after one-off use.

### 2.5 Anti-patterns

Do not:

- rebuild everything for a small request
- add schema casually
- collapse branch campuses without review
- replace `university_id` with an external key anywhere
- mix 6-digit CIP logic with 4-digit outcomes without an explicit bridge
- describe USAspending, NIH, or NSF as the authority for institution identity
- rank schools purely by grant dollars or publication count
- hide ambiguity instead of surfacing it
- treat institution-level outcomes as field-specific when field-level data exist
- run unfiltered BigQuery scans against the full OpenAlex works table — always filter year/type before unnesting authorships
- omit `maximum_bytes_billed` on any BigQuery job

---

## 3. Identity model

### 3.1 Canonical key

`university_id` — an internal integer primary key — is the sole project-wide canonical key.

External identifiers live in `university_identifiers` and are never used as the primary key elsewhere.

### 3.2 External identifier systems

| System code       | Authority                    | Role in project                                        |
| ----------------- | ---------------------------- | ------------------------------------------------------ |
| `IPEDS`           | NCES                         | Seeded-universe identity backbone, completions         |
| `SCORECARD_UNITID`| Dept. of Ed                  | Scorecard institution join                             |
| `SCORECARD_OPEID6`| OPE / FSA                    | Scorecard field-of-study join                          |
| `ROR`             | ROR registry                 | Research-side institutional anchor                     |
| `OPENALEX`        | OpenAlex                     | Publication data join                                  |
| `UEI`             | SAM.gov / GSA                | Federal awards (USAspending, NIH, NSF)                 |
| `DUNS`            | Dun & Bradstreet (legacy)    | Historical federal awards before UEI transition        |

**Identifier authority vs. lookup surface**: NIH RePORTER, USAspending, and NSF expose downstream copies of UEI/DUNS. They are not the authority for identity mapping and should not be described as such.

### 3.3 Identity resolution strategy

Resolution order:

1. Seed from IPEDS `UNITID` (backbone).
2. Map `UNITID → Scorecard UNITID/OPEID6` (near 1-to-1 for most bachelor's-granting institutions; watch parent–child OPEID rollups).
3. Map institution name + domain + location → `ROR` using ROR API fuzzy matching. Require human review for confidence < 0.9 or multiple candidates.
4. Map `ROR → OpenAlex institution` via OpenAlex's built-in ROR field (deterministic for institutions that have a ROR).
5. Map to `UEI` via SAM.gov entity registration data or the ROR → UEI crosswalk where it exists.

**Known failure modes** (must route to review queue):

- Branch campuses vs. parent institutions (IPEDS may have separate UNITIDs; Scorecard may roll them up under one OPEID6).
- Separate medical schools or research institutes that share a university name.
- Multiple UEIs for one university system.
- Name variants across IPEDS, ROR, OpenAlex, NIH, NSF, and USAspending.
- Scorecard OPEID6 rollups that do not cleanly map to a single IPEDS unit.

---

## 4. Granularity rules

Three separate granularity layers exist and must never be conflated:

| Layer                          | Native grain                          | Primary source             |
| ------------------------------ | ------------------------------------- | -------------------------- |
| Education offering/completions | 6-digit CIP                           | IPEDS                      |
| Field-of-study outcomes        | 4-digit CIP + credential level        | College Scorecard FOS file |
| Research subject               | OpenAlex subfield (254 subfields)     | OpenAlex                   |

Required bridges:

- `6-digit CIP → 4-digit CIP` truncation for Scorecard field-outcome joins.
- `6-digit CIP → OpenAlex subfield(s)` via `cip_trains_subfield` mapping table.
- `OpenAlex topic → subfield → field → domain` roll-up within the OpenAlex hierarchy itself (4 domains, 26 fields, 254 subfields, ~4,500 topics).

Every pipeline step that crosses a layer boundary must do so explicitly and traceably.

---

## 5. Data source inventory

### 5.1 IPEDS — Institution universe, characteristics, completions

**What it provides**: universe of ~6,400 Title IV participating institutions; institutional characteristics (sector, control, Carnegie classification, state, city, URLs); completions by CIP code, award level, and year; enrollment; graduation rates.

**Role**: Seeds the candidate institution universe. Proves a target program exists and is active. Provides completion scale and persistence over time.

**Format and access**: Annual bulk CSV files in zipped archives from the NCES Data Center (`nces.ed.gov/ipeds`). Each survey year is split into ~20–30 separate CSV files (HD = directory, IC = institutional characteristics, C\_A = completions by CIP, EFFY = enrollment, GR = graduation rates, etc.). Dictionaries in XLS/XLSX. Variable names change between years — always load the corresponding data dictionary.

**Key files for this project**:

| IPEDS file prefix | Content                                    |
| ----------------- | ------------------------------------------ |
| `HD`              | Directory (name, address, sector, control) |
| `IC`              | Institutional characteristics, programs    |
| `C_A` / `C_B`    | Completions by CIP, award level, race/sex  |
| `EFFY`            | 12-month enrollment                        |
| `GR`              | Graduation rates (150%, 200%)              |
| `SFA`             | Student financial aid, net price           |

**Join key**: `UNITID` (integer, stable across years).

**Gotchas**: Variable names drift across years. For-profit schools and non-degree-granting institutions appear in the universe and must be filtered. Some schools report combined completions under a system-level UNITID.

### 5.2 College Scorecard — Student outcomes, admissions, cost

#### 5.2a Institution-level data

**What it provides**: Admissions rate, SAT/ACT scores, net price by income band, completion rate, retention rate, institution-level median earnings (6, 8, 10 years after entry and 1, 5 years after graduation), median debt, Pell share, first-generation share.

**Role**: Student-facing backbone. Institution-wide fallback when field-level outcomes are missing.

**Format and access**: Bulk CSV files downloadable from `collegescorecard.ed.gov/data/`. Also available via REST API (JSON) at `api.data.gov/ed/collegescorecard/v1/schools` (requires free API key; 1,000 req/hr rate limit). Data dictionary in XLSX with cohort maps showing which year each variable reflects.

**Join keys**: `UNITID` (maps to IPEDS), `OPEID6` (6-digit OPE ID used for field-of-study data).

**Gotchas**: Privacy suppression (coded `PS`) hides values for cells < 30 students. `latest.*` fields in the API mix values from different data years. Parent–child OPEID structures allocate proportionally — verify that the allocation is sensible for your institution.

#### 5.2b Field-of-study data

**What it provides**: Median earnings 1 and 5 years after graduation, median debt, and completers count — all at `OPEID6 × 4-digit CIP × credential level` grain.

**Role**: The strongest public signal for undergraduate payoff by field. Critical for field-specific outcome scoring.

**Format and access**: Separate bulk CSV file (`FieldOfStudyData`), also queryable via the same Scorecard API.

**Join keys**: `OPEID6` (not UNITID), `CIPCODE` (4-digit), `CREDLEV` (credential level: 3 = bachelor's for this project).

**Gotchas**: Uses 4-digit CIP, not 6-digit — requires a truncation bridge from IPEDS completions. Heavy privacy suppression in smaller programs. Credential level mapping between NSLDS and IPEDS is not perfectly aligned (see Scorecard documentation Exhibit 1).

### 5.3 OpenAlex — Research output and subject classification

**What it provides**: ~460M+ scholarly works (post-Walden rewrite, Nov 2025) with author affiliations, institutional linkages (via ROR), topic/subfield classification (4,500 topics → 254 subfields → 26 fields → 4 domains), citation counts, and citation-impact percentiles (`citation_normalized_percentile`).

**Role**: Research opportunity and field-fit backbone. Measures each university's publication output and citation impact in target scholarly areas.

#### 5.3.1 Access tiers — when to use which

| Tier | Backend | When to use |
| ---- | ------- | ----------- |
| **Primary** | OrionDB / BigQuery | Multi-institution, multi-year, multi-cluster extraction; any analysis requiring work-level joins before aggregation; citation-impact metrics; repeated runs under different configs. **This is the default for all non-trivial research-impact work.** |
| **Fallback** | Local snapshot | Debugging, validation, narrow offline work, or explicit local-only preference. The snapshot is ~330 GB compressed and requires significant local infrastructure. |
| **Spot-check** | REST API | Single-institution lookups, incremental validation, quick exploration. |

OrionDB / BigQuery is primary because research-impact analysis at scale is warehouse-style work. Downloading, decompressing, parsing, and joining 330 GB of JSONL locally is viable but slow and expensive in developer time. BigQuery makes the same work a filtered SQL query with results in seconds.

#### 5.3.2 OrionDB / BigQuery specifics

**What is OrionDB**: ORION-DBs (`orion-dbs.community`) is a community platform hosted by CWTS Leiden and SUB Göttingen that provides time-versioned OpenAlex snapshots on Google BigQuery, along with Leiden Ranking Open Edition data and other scholarly datasets. Tables are publicly queryable by anyone with a Google Cloud project.

**BigQuery project and dataset**: `subugoe-collaborative.openalex.*` — contains `works`, `institutions`, `sources`, and other entity tables mirroring the OpenAlex snapshot structure. CWTS Leiden collections provide additional datasets including the Leiden Ranking.

**Prerequisite**: A Google Cloud project (the BigQuery sandbox requires no billing account and includes 1 TB of free queries per month). Beyond the sandbox, BigQuery charges $6.25 per TB of data processed.

**Key tables for this project**:

| BQ table | Key columns for our use |
| -------- | ----------------------- |
| `subugoe-collaborative.openalex.works` | `id`, `doi`, `publication_year`, `primary_topic`, `topics`, `authorships` (REPEATED RECORD with nested `institutions`), `cited_by_count`, `citation_normalized_percentile`, `type`, `is_paratext`, `is_retracted` |
| `subugoe-collaborative.openalex.institutions` | `id`, `ror`, `display_name`, `country_code`, `type` |

**Authorships are nested**: Each work has an `authorships` REPEATED field. Each authorship contains `institutions` (also REPEATED). To join works to institutions you must `UNNEST(authorships) AS au, UNNEST(au.institutions) AS inst`. This unnest is expensive — delay it until after year/type filters are applied.

#### 5.3.3 BigQuery extraction pattern

This is the expected sequence for a coding assistant extracting research metrics:

1. **Build local filter sets** in Python/DuckDB:
   - Candidate `university_id` → resolved OpenAlex institution IDs
   - Target subfield IDs (from `cip_trains_subfield` resolution)
   - Year range from config

2. **Push filter sets to BigQuery** as temporary tables, `UNNEST`-ed arrays, or `CREATE TEMP TABLE` from literal values. Keep these small (hundreds to low-thousands of rows).

3. **Query works with major filters first**:
   ```sql
   -- Step 1: Narrow works before expensive unnest
   CREATE TEMP TABLE filtered_works AS
   SELECT w.id, w.publication_year, w.cited_by_count,
          w.citation_normalized_percentile,
          w.primary_topic, w.topics, w.authorships
   FROM `subugoe-collaborative.openalex.works` w
   WHERE w.publication_year BETWEEN @start_year AND @end_year
     AND w.is_paratext = FALSE
     AND w.is_retracted = FALSE
     AND w.type IN ('article', 'review', 'preprint');

   -- Step 2: Now unnest and filter by institution + subfield
   CREATE TEMP TABLE matched_works AS
   SELECT fw.id AS work_id,
          fw.publication_year,
          fw.citation_normalized_percentile,
          inst.id AS institution_id,
          fw.primary_topic.subfield.id AS primary_subfield_id
   FROM filtered_works fw,
        UNNEST(fw.authorships) AS au,
        UNNEST(au.institutions) AS inst
   WHERE inst.id IN UNNEST(@candidate_openalex_ids);
   ```

4. **Aggregate to the target grain**:
   ```sql
   SELECT institution_id, primary_subfield_id, publication_year,
          'primary_subfield' AS counting_method,
          COUNT(DISTINCT work_id) AS publications_count,
          AVG(citation_normalized_percentile.value) AS avg_citation_impact
   FROM matched_works
   WHERE primary_subfield_id IN UNNEST(@target_subfield_ids)
   GROUP BY institution_id, primary_subfield_id, publication_year;
   ```

5. **Export to Parquet** (Snappy-compressed) via `bq extract` or `client.query(...).to_dataframe().to_parquet(...)`. Organize by `run_id` and artifact name.

6. **Load Parquet into local DuckDB/SQLite staging**, map OpenAlex institution IDs back to `university_id`, and upsert into `university_subfield_year_metrics`.

#### 5.3.4 BigQuery cost discipline

Every BigQuery query against OpenAlex must follow these rules:

- **Dry run first**: Use `job_config.dry_run = True` (Python) or `--dry_run` (bq CLI) to check bytes scanned before executing.
- **Set a byte cap**: Always set `maximum_bytes_billed` (e.g., 10 GB for a typical institution-filtered query). A full unfiltered scan of `works` can exceed 1 TB.
- **Filter early**: Apply `publication_year`, `is_paratext`, `is_retracted`, and `type` filters before any `UNNEST` of authorships or topics.
- **Filter by institution before subfield**: The institution filter dramatically reduces the working set; the subfield filter then refines it.
- **Minimize columns**: `SELECT` only the columns needed. BQ charges by bytes scanned, and each unused column in the `SELECT` costs real money.
- **Materialize intermediates**: When the same filtered subset is used for multiple aggregations (e.g., different counting methods), materialize it as a temp table rather than re-scanning works.
- **Log cost in the run manifest**: Record `total_bytes_processed`, `total_bytes_billed`, BQ job IDs, and export paths.

#### 5.3.5 Local snapshot fallback

**Format**: ~330 GB gzip-compressed JSONL partitioned by `updated_date` on AWS S3 (`s3://openalex`). Download via `aws s3 sync --no-sign-request`. Each entity is one JSON line. Incremental updates by downloading only new `updated_date` partitions.

**When to use**: Debugging, local validation of BQ results, environments without GCP access, or user-requested offline mode.

#### 5.3.6 REST API (spot checks)

REST at `api.openalex.org`. Filter works by institution, topic, subfield, publication year. Group-by endpoint useful for quick aggregation. Polite pool: include `mailto:` in requests. Rate limit: ~10 req/sec unauthenticated.

#### 5.3.7 Key entity relationships

```
Work → authorships[] → institutions[] (via ROR)
Work → primary_topic → subfield → field → domain
Work → topics[] (up to 3 scored topics per work)
```

**Join keys**: OpenAlex institution ID (maps to ROR), subfield ID (integer), topic ID.

#### 5.3.8 Counting methods (must be tracked separately)

- `primary_subfield`: Count a work once under its `primary_topic.subfield`.
- `topics_any`: Count a work once for each subfield appearing in its `topics[]` array.
- `topic_deduped_subfield`: Count a work once per distinct subfield across all its scored topics.

Never mix counting methods in a single metric series.

#### 5.3.9 Gotchas

- Not all works have topics assigned (requires title, abstract, or citations).
- The topic model is citation-cluster-based, not journal-based — a work's subfield may differ from its journal's expected subject.
- Older works (pre-2000) have sparser metadata.
- The Walden rewrite (Nov 2025) changed affiliation metadata coverage in some cases — spot-check institution coverage for recent years.
- UNNEST of authorships in BQ is the most expensive operation — always filter year/type first.

### 5.4 ROR — Research Organization Registry

**What it provides**: Canonical identifiers, names (including aliases), locations, types, and relationships for ~110,000 research organizations worldwide.

**Role**: Institutional identity anchor for the research side. Bridges IPEDS universe to OpenAlex.

**Format and access**: JSON data dump (downloadable from Zenodo or `ror.org/data`). REST API at `api.ror.org` supporting fuzzy name matching (`?query=`) and affiliation matching (`?affiliation=`).

**Join keys**: ROR ID (URL-format, e.g., `https://ror.org/03vek6s52`). OpenAlex institutions expose their ROR ID directly.

**Gotchas**: ROR includes non-U.S. and non-degree-granting entities. Affiliation matching may return multiple candidates for common university names. Confidence score from the API should be used to route ambiguous matches to the review queue.

### 5.5 USAspending — Federal awards context

**What it provides**: Total federal award dollars by recipient, awarding agency, year, and CFDA program.

**Role**: Top-line federal funding context. Not a scientific field taxonomy — do not use for subfield-level grant attribution unless explicitly justified.

**Format and access**: REST API at `api.usaspending.gov` (JSON). Also bulk download (very large — prefer API for institution-level totals). Key endpoints: `/api/v2/search/spending_by_award/`, `/api/v2/recipient/`.

**Join keys**: Recipient UEI (post-2022), Recipient DUNS (pre-2022), parent recipient fields for university system rollups.

**Gotchas**: Recipient names are noisy. A single university may appear under multiple UEIs (main campus, hospital, research foundation). Always verify identity against the canonical `university_id` before loading. Dollar amounts may include sub-awards — check award type filters.

### 5.6 NIH RePORTER / ExPORTER — Biomedical funding detail

**What it provides**: NIH project-level data: award amounts, activity codes, institute/center, project terms, PI affiliation, linked publications.

**Role**: High-value detail layer for biomedical field clusters (MolBio, Genetics, Biochemistry). Provides agency-level funding totals by institution.

**Format and access**: REST API at `reporter.nih.gov/api`. Bulk ExPORTER files (CSV) downloadable from `reporter.nih.gov/exporter`. Link tables connect projects to publications.

**Join keys**: Organization UEI (current), Organization DUNS (legacy), Organization Name.

**Gotchas**: NIH organizational identifiers are downstream copies — NIH is not the authority for institutional identity. Activity codes (R01, T32, P01, etc.) can distinguish research grants from training grants, which matters for interpreting undergraduate-relevant funding. Medical schools may report separately from parent universities.

### 5.7 NSF Award Search — Science funding context

**What it provides**: NSF award-level data: amounts, start/end dates, directorate/division, PI institution.

**Role**: Science-specific funding context, especially for Statistics, Mathematics, Computer Science, and adjacent quantitative clusters.

**Format and access**: REST API at `api.nsf.gov/services/v1/awards` (JSON/XML). Bulk download available.

**Join keys**: Awardee name + state (noisy), sometimes UEI if present.

**Gotchas**: NSF join quality is the worst of the three grant sources because the API lacks a reliable institutional ID field — name + location matching is required and must be validated. Division/directorate metadata is the only subject-area signal, and it is coarser than OpenAlex subfields.

### 5.8 CIP-to-OpenAlex Subfield Mapping (internal)

**What it provides**: The intellectual bridge between undergraduate instructional programs (CIP codes) and scholarly research areas (OpenAlex subfields).

**Role**: Critical mapping layer that defines which research subfields are relevant to each CIP and at what fit level (`primary`, `secondary`, `adjacent`).

**Source of truth**: `cip_to_openalex_subfields_openalex_native.md` in this repo. Do NOT use `CIP_to_openalex_subfields_with_fit_level.xlsx` unless the user explicitly asks to compare or migrate.

**Format**: Markdown table; loaded into `cip_trains_subfield` in the database.

### 5.9 Optional: Common Data Set

**What it provides**: Self-reported institutional data including undergraduate research opportunity flags, study abroad, internships, selectivity details.

**Role**: Enrichment only, applied to top-ranked candidates after the core pipeline is stable. Never the primary backbone.

**Format**: Decentralized PDFs and web pages — semi-structured, manual extraction required.

---

## 6. Field cluster model

A field cluster is the unit of ranking. It is defined in the pipeline's YAML config and bundles:

- Target instructional programs (6-digit CIP codes)
- Optional CIP family expansion (2-digit prefix)
- Explicit CIP exclusions
- Mapped OpenAlex subfields (populated directly or derived via `cip_trains_subfield`)
- Fit-level weights for subfield aggregation (`primary`, `secondary`, `adjacent`)
- Scoring profile override (or default to `balanced`)

### 6.1 Conceptual separation

Keep separate:

- **What students can major in** (CIP-based, from IPEDS completions)
- **What research the institution does** (subfield-based, from OpenAlex)

A school may publish heavily in a research area without offering a strong undergraduate major, and vice versa.

### 6.2 Mapping logic per field cluster

1. Start from config-defined `include_6digit` CIPs.
2. Optionally expand via `include_2digit_families` — this must not overwhelm the explicit list.
3. Apply `exclude_6digit` to remove unwanted CIPs.
4. Truncate 6-digit CIPs to 4-digit groups for Scorecard field-outcome joins.
5. Resolve OpenAlex subfields via `cip_trains_subfield`, applying `fit_weights` for research aggregation.
6. Store the fully resolved CIP set and subfield set per cluster for inspection.

### 6.3 Field-definition drift

Fields like MolBio, Genetics, Biochemistry, Bioinformatics, Applied Math, Statistics, and Data Science overlap. The config must make the project's boundary explicit and inspectable. The pipeline must never silently expand or contract a cluster's scope.

---

## 7. Persisted schema (database)

The database stores normalized, reusable entities and mappings. Ranking features and outputs are derived artifacts (§9) and do not live in the database unless explicitly requested.

Use SQLite with `PRAGMA foreign_keys = ON`.

### 7.1 Core entity tables

**`universities`** — One row per canonical institution.

| Column          | Type    | Notes                               |
| --------------- | ------- | ----------------------------------- |
| `university_id` | INTEGER | PK, internal canonical key          |
| `canonical_name`| TEXT    | Project-preferred display name      |
| `country_code`  | TEXT    |                                     |
| `state_code`    | TEXT    | nullable                            |
| `city`          | TEXT    | nullable                            |
| `homepage_url`  | TEXT    | nullable                            |
| `control`       | TEXT    | nullable (public/private-nonprofit) |
| `is_active`     | INTEGER | nullable, default 1                 |

Do not create duplicate rows for name variants across systems.

**`identifier_systems`** — Dimension table for external ID types.

| Column           | Type    | Notes                                  |
| ---------------- | ------- | -------------------------------------- |
| `system_code`    | TEXT    | PK (`IPEDS`, `ROR`, `OPENALEX`, etc.) |
| `system_name`    | TEXT    |                                        |
| `is_authoritative` | INTEGER | nullable                             |
| `notes`          | TEXT    | nullable                               |

**`university_identifiers`** — Each university's ID in each external system.

| Column          | Type    | Notes                                             |
| --------------- | ------- | ------------------------------------------------- |
| `university_id` | INTEGER | FK → universities                                 |
| `system_code`   | TEXT    | FK → identifier_systems                           |
| `external_id`   | TEXT    |                                                   |
| `external_url`  | TEXT    | nullable                                          |
| `match_status`  | TEXT    | nullable (`exact`, `reviewed`, `ambiguous`, `provisional`) |
| `source_note`   | TEXT    | nullable                                          |

PK: `(university_id, system_code)`. Unique: `(system_code, external_id)`.

### 7.2 Subject dimension tables

**`cips`** — CIP codes and titles.

| Column            | Type    | Notes                          |
| ----------------- | ------- | ------------------------------ |
| `cip_code`        | TEXT    | PK — preserve dots and leading zeros |
| `cip_title`       | TEXT    |                                |
| `cip_level`       | INTEGER | nullable (2, 4, or 6 digit)   |
| `cip_family_code` | TEXT    | nullable (2-digit parent)      |
| `is_active`       | INTEGER | nullable                       |

**`oa_domains`**, **`oa_fields`**, **`oa_subfields`** — OpenAlex four-level hierarchy (domain → field → subfield; topics are not stored here since they are too granular for ranking).

| Table          | PK            | FK            | Name column      |
| -------------- | ------------- | ------------- | ---------------- |
| `oa_domains`   | `domain_id`   | —             | `domain_name`    |
| `oa_fields`    | `field_id`    | `domain_id`   | `field_name`     |
| `oa_subfields` | `subfield_id` | `field_id`    | `subfield_name`  |

All IDs are INTEGER matching OpenAlex's integer ID scheme.

### 7.3 Mapping tables

**`university_cip_offers`** — Which schools offer which programs.

| Column          | Type    | Notes                              |
| --------------- | ------- | ---------------------------------- |
| `university_id` | INTEGER | FK                                 |
| `cip_code`      | TEXT    | FK                                 |
| `award_level`   | TEXT    | nullable (bachelor's, master's...) |
| `first_year`    | INTEGER | nullable                           |
| `last_year`     | INTEGER | nullable                           |
| `source_system` | TEXT    |                                    |
| `source_note`   | TEXT    | nullable                           |

PK: `(university_id, cip_code)`.

**`cip_trains_subfield`** — The key intellectual bridge between education and research.

| Column           | Type    | Notes                                    |
| ---------------- | ------- | ---------------------------------------- |
| `cip_code`       | TEXT    | FK                                       |
| `subfield_id`    | INTEGER | FK                                       |
| `fit_level`      | TEXT    | `primary`, `secondary`, or `adjacent`    |
| `confidence`     | REAL    | nullable                                 |
| `rationale`      | TEXT    | nullable                                 |
| `mapping_source` | TEXT    | nullable                                 |

PK: `(cip_code, subfield_id)`.

### 7.4 Research metrics

**`university_subfield_year_metrics`** — Yearly research output by university and OpenAlex subfield.

| Column             | Type    | Notes                                         |
| ------------------ | ------- | --------------------------------------------- |
| `university_id`    | INTEGER | FK                                            |
| `subfield_id`      | INTEGER | FK                                            |
| `year`             | INTEGER |                                               |
| `counting_method`  | TEXT    | `primary_subfield`, `topics_any`, etc.        |
| `publications_count` | INTEGER |                                              |
| `avg_citation_impact` | REAL  | nullable                                      |
| `impact_metric_name` | TEXT   | nullable (e.g., `citation_normalized_percentile`) |
| `source_snapshot`  | TEXT    | nullable                                      |
| `source_note`      | TEXT    | nullable                                      |

PK: `(university_id, subfield_id, year, counting_method)`.

### 7.5 Grant tables

**`grant_sources`** — Funder dimension.

| Column           | Type    | Notes                      |
| ---------------- | ------- | -------------------------- |
| `grant_source_id`| INTEGER | PK                         |
| `source_code`    | TEXT    | `NIH`, `NSF`, `USASPENDING_ALL_FEDERAL` |
| `source_name`    | TEXT    |                            |
| `country_code`   | TEXT    | nullable                   |
| `is_us_federal`  | INTEGER | nullable                   |

**`university_grant_year_stats`** — Top-line yearly grant totals.

| Column             | Type    | Notes |
| ------------------ | ------- | ----- |
| `university_id`    | INTEGER | FK    |
| `grant_source_id`  | INTEGER | FK    |
| `year`             | INTEGER |       |
| `grant_count`      | INTEGER | nullable |
| `total_award_usd`  | REAL    | nullable |
| `source_snapshot`  | TEXT    | nullable |

PK: `(university_id, grant_source_id, year)`.

**`university_grant_subfield_year_stats`** — Subfield-attributed grants. **Only populate when attribution is methodologically defensible.** Do not guess.

PK: `(university_id, grant_source_id, subfield_id, year)`. Same columns as above plus `subfield_id`, `mapping_method`.

### 7.6 Relationship summary

```
universities
  ├── university_identifiers → identifier_systems
  ├── university_cip_offers → cips → cip_trains_subfield → oa_subfields → oa_fields → oa_domains
  ├── university_subfield_year_metrics → oa_subfields
  ├── university_grant_year_stats → grant_sources
  └── university_grant_subfield_year_stats → grant_sources + oa_subfields
```

---

## 8. Pipeline stages

Implement as a sequence of narrow, inspectable tasks. Each stage has defined inputs, outputs, and validation criteria.

### Stage 0 — Setup and sanity checks

**Goal**: Verify repo state before any writes.

**Tasks**:
- Confirm target tables exist in the database.
- Inspect current row counts and data freshness.
- Determine whether run is incremental or fresh.
- Validate that config YAML parses correctly.

**Outputs**: Setup log, source availability report.

### Stage 1 — Build candidate institution universe

**Goal**: Establish the base set of rankable institutions from the repo's seeded universe, then backfill IPEDS coverage for that fixed scope.

**Source**: Repo seed input for the institution list, plus IPEDS HD (directory) and IC (characteristics) files for enrichment and validation.

**Tasks**:
- Load the seeded institution list that defines project scope.
- Ingest IPEDS institution directory for the configured `candidate_universe_asof` year only to resolve and validate those seeded institutions.
- Apply any config sector/enrollment filters only as validation or exclusion checks within the seeded set, not as a nationwide discovery step.
- Upsert `universities` and `university_identifiers` (especially `IPEDS UNITID`) for the seeded institutions that can be resolved cleanly.

**Outputs**: Seeded candidate-universe extract; counts by sector/state/control; unresolved seeded institutions for review.

**Validation**: Expect row counts to match the intended seeded universe size for the run. Spot-check 10 known schools and any unresolved seeded institutions.

### Stage 2 — Resolve institutional identity across systems

**Goal**: Connect candidates to Scorecard, ROR, OpenAlex, and grant-side identifiers.

**Tasks**:
1. Map `UNITID → Scorecard UNITID/OPEID6` (near-deterministic for most institutions).
2. Fuzzy-match institution name + domain + location → ROR via ROR API.
3. Map `ROR → OpenAlex institution ID` (OpenAlex exposes ROR directly).
4. Attach UEI from SAM.gov or ROR–UEI crosswalk where available.
5. Route ambiguous or low-confidence matches to review queue.

**Outputs**: Populated `university_identifiers`; identity review queue CSV.

**Validation**: ≥90% of candidate universities should have exact Scorecard and ROR matches. All ambiguous cases must appear in the review queue.

### Stage 3 — Load reference dimensions

**Goal**: Populate CIP and OpenAlex subject-hierarchy tables.

**Sources**: IPEDS CIP taxonomy file; OpenAlex `/subfields`, `/fields`, `/domains` endpoints (or snapshot).

**Tasks**:
- Load CIP codes and titles into `cips` (TEXT type for `cip_code`).
- Load OpenAlex domains, fields, subfields into `oa_domains`, `oa_fields`, `oa_subfields`.
- Validate uniqueness and FK integrity.

**Outputs**: Refreshed reference tables.

**Validation**: 254 subfields, 26 fields, 4 domains. CIP table should have ~2,000+ 6-digit codes.

### Stage 4 — Load educational offerings and completions

**Goal**: Determine which schools offer target programs and at what scale.

**Source**: IPEDS C\_A (completions) files for configured year range.

**Tasks**:
- Ingest completions by `UNITID × CIP × award level × year`.
- Filter to bachelor's-level awards.
- Derive offering persistence (how many years a CIP appears) and recent completion totals.
- Populate `university_cip_offers`.

**Outputs**: `university_cip_offers`; annual completions extracts; field-family coverage report.

**Validation**: Spot-check 5 known programs (e.g., MIT Statistics, Stanford MolBio) to confirm they appear with plausible completion counts.

### Stage 5 — Resolve CIP-to-subfield mappings

**Goal**: Connect instructional programs to research areas.

**Source**: `cip_to_openalex_subfields_openalex_native.md`.

**Tasks**:
- Parse the mapping file.
- Populate `cip_trains_subfield` with fit levels.
- Validate that every CIP in the configured field clusters has at least one mapped subfield.
- Emit unmapped-CIP report for any gaps.

**Outputs**: `cip_trains_subfield`; unmapped-CIP report.

**Validation**: Zero unmapped target CIPs (or all unmapped ones documented in the report).

### Stage 6 — Compute research metrics

**Goal**: Measure publication output and citation impact in target areas.

**Source**: OrionDB / BigQuery-hosted OpenAlex (primary); local snapshot (fallback). See §5.3 for the full decision framework.

**Tasks**:
1. Assemble local filter sets: candidate OpenAlex institution IDs, target subfield IDs, year range.
2. Push filter sets to BigQuery as temp tables or parameterized arrays.
3. Execute the extraction pattern from §5.3.3: filter works → unnest authorships → match institutions → aggregate by `university × subfield × year × counting_method`.
4. Run dry-run first; enforce `maximum_bytes_billed`; log bytes processed.
5. Export aggregated result to Parquet (Snappy-compressed), organized under `outputs/{run_id}/`.
6. Load Parquet into local DuckDB/SQLite staging.
7. Map OpenAlex institution IDs back to `university_id` via `university_identifiers`.
8. Upsert into `university_subfield_year_metrics`.

**Cost guard**: A typical extraction for ~2,500 institutions × 5 years × 10 subfields should scan < 50 GB on BQ if filters are applied correctly. If a dry run reports > 100 GB, re-examine the query plan.

**Outputs**: `university_subfield_year_metrics`; Parquet export artifacts; extraction manifest (BQ job ID, bytes processed, snapshot version, counting methods, export paths).

**Validation**: Top research universities (e.g., Harvard, MIT, Stanford) should show substantial publication counts in expected subfields. No university should show implausibly high counts that suggest a system-level rollup bug. Cross-check a sample of 5 institutions against the OpenAlex API `group_by` endpoint to confirm BQ aggregates match.

### Stage 7 — Load funding context

**Goal**: Add federal funding context without overclaiming precision.

**Sources**: USAspending API, NIH RePORTER API/bulk, NSF Awards API.

**Tasks**:
- Aggregate USAspending totals by institution × year (use UEI join).
- Aggregate NIH totals by institution × year.
- Aggregate NSF totals by institution × year.
- Populate `grant_sources` and `university_grant_year_stats`.
- Only populate `university_grant_subfield_year_stats` where subfield attribution is defensible (e.g., NIH institute-to-subfield mapping for biomedical clusters). Otherwise, leave it empty.

**Outputs**: Grant summary tables; methodology notes for any subfield attribution.

**Validation**: Top NIH-funded schools (Johns Hopkins, UPenn, UCSF) should appear in the top 20 for NIH totals.

### Stage 8 — Load student outcomes and value data

**Goal**: Attach field-specific and institution-level undergraduate outcomes.

**Sources**: College Scorecard institution-level CSV, field-of-study CSV.

**Tasks**:
- Ingest institution-level metrics: admissions rate, net price, completion rate, retention rate, median earnings, debt, Pell share.
- Ingest field-of-study metrics: earnings, debt, completers at `OPEID6 × 4-digit CIP × credential level (bachelor's)`.
- Build explicit `6-digit CIP → 4-digit CIP` bridge for field-outcome joins.
- Compute field-cluster-level outcome aggregates (weighted by completer counts where multiple 4-digit CIPs map to one cluster).

**Outputs**: Derived outcomes feature set (Parquet); missing-field-outcomes coverage report.

**Validation**: Field-outcome coverage will be patchy (expect 40–70% of institutions × clusters to have field-level earnings). All missing cases must be documented.

### Stage 9 — Build feature store

**Goal**: Assemble all scoring inputs into one analytical table.

**Grain**: One row per `university_id × field_cluster_slug`.

**Tasks**:
- Expand each configured field cluster into its resolved CIP set and subfield set.
- Compute feature families (see §8.1 below).
- Apply fallback rules: institution-level outcomes where field-level are missing, with confidence penalty.
- Compute confidence sub-features.
- Write provenance and fallback flags.

**Feature families**:

**A. Program availability and scale** — confirms the school actually offers the target area.

- `offers_any_target_cip` (boolean)
- `target_cip_count` (integer)
- `recent_bachelors_completions_target` (sum over lookback window)
- `recent_bachelors_completions_4digit_group` (broader 4-digit rollup)
- `target_completions_share_of_all_bachelors` (ratio)
- `program_persistence_years` (how many of the last N years the CIP was awarded)
- `min_recent_bachelors_threshold_passed` (boolean, per config threshold)

**B. Research strength and field fit** — estimates research opportunity density and quality.

- `oa_publications_primary_fit` (publications in primary-fit subfields)
- `oa_publications_secondary_fit`
- `oa_publications_adjacent_fit`
- `oa_publications_weighted_fit` (fit-weight-adjusted total)
- `oa_avg_citation_impact_weighted` (fit-weighted mean citation percentile)
- `oa_recent_trend_slope` (publication trend over configured window)
- `research_strength_percentile_within_field_cluster`

**C. Grant context** — funding context, not standalone quality proxy.

- `federal_grants_total_5y`
- `nih_grants_total_5y`
- `nsf_grants_total_5y`
- `target_proximate_grants_total_5y` (only when defensible)
- `grant_intensity_per_publication` (optional ratio)

**D. Student outcomes** — measures undergraduate payoff.

- `scorecard_field_median_earnings` (field-level if available)
- `scorecard_field_median_debt`
- `scorecard_field_completers`
- `scorecard_institution_completion_rate` (used as fallback or supplement)
- `scorecard_institution_retention_rate`
- `field_outcome_available` (boolean flag)
- `using_institution_fallback` (boolean flag)

**E. Affordability and value** — penalizes cost unless outcomes justify it.

- `avg_annual_cost` (net price)
- `net_price_by_income_band` (where available)
- `debt_to_earnings_proxy`

**F. Selectivity and access** — admissions context.

- `admission_rate`
- `test_score_context` (SAT/ACT midpoints)
- `pell_share`
- `selectivity_band` (derived categorical)

**G. Enrichment** (optional, post-first-pass).

- `has_undergrad_research_flag`
- `has_internship_flag`
- `has_study_abroad_flag`

**Outputs**: Feature-store Parquet; audit columns showing provenance and fallback flags.

### Stage 10 — Score and rank

**Goal**: Produce final rankings and explainability fields.

**Scoring model**: Profile-driven, normalized within each field cluster (not across clusters).

**Default balanced profile**:

```
overall_balanced_score =
    0.20 × program_availability_score
  + 0.25 × research_strength_score
  + 0.25 × student_outcomes_score
  + 0.15 × affordability_value_score
  + 0.10 × selectivity_access_score
  + 0.05 × enrichment_score
```

Alternative profiles (`research_heavy`, `value_heavy`) adjust weights per the config.

**Confidence score** declines when:
- Field-level outcomes are missing (institution fallback used)
- Identity matching is ambiguous
- CIP-to-subfield mapping is sparse or low-confidence
- Target completions are very small
- Research metrics are thin or noisy

**Tasks**:
- Normalize raw features into 0–100 percentile scores within each cluster.
- Apply configured weight profile.
- Compute component scores, overall score, and confidence score.
- Generate ranks within each field cluster.
- Create top-N summaries with reason strings explaining each school's ranking.

**Outputs**: Rankings Parquet + CSV; top-N summaries by cluster.

### Stage 11 — QA, coverage, and sensitivity checks

**Goal**: Ensure outputs are plausible and stable.

**Tasks**:
- Validate top-25 results per cluster for face plausibility (are known strong programs present?).
- Re-run under alternative weight profiles and report rank movement.
- Compute coverage rates for each source and feature family.
- Write unresolved-identity and unmapped-CIP reports.
- Flag suspicious outputs (e.g., school ranks top-10 but confidence < 0.3).

**Outputs**: QA report, sensitivity report, manual review queue.

---

## 9. Derived artifacts (not persisted in database)

These are pipeline outputs. They live outside the normalized schema unless explicitly requested.

| Artifact                  | Format               | Grain                                    |
| ------------------------- | -------------------- | ---------------------------------------- |
| Field-cluster config      | YAML                 | One entry per `field_cluster_slug`        |
| Candidate universe        | Parquet or SQL view  | One row per `university_id`               |
| Feature matrix            | Parquet              | One row per `university_id × cluster`     |
| Ranking output            | Parquet + CSV        | One row per `university_id × cluster`     |
| QA outputs                | CSV                  | Various (review queues, coverage reports) |
| Run manifest              | YAML or JSON         | One per pipeline run                      |

### 9.1 Run manifest contents

- Extraction dates per source
- Source versions / snapshot identifiers
- OpenAlex backend used (`bigquery_primary` or `snapshot_fallback`)
- BigQuery job metadata (bytes processed, bytes billed, job IDs) when applicable
- BQ export paths and Parquet artifact locations
- Year windows used
- Config hash
- Code revision
- Row counts by stage

---

## 10. Missing-data and ambiguity policy

### 10.1 Exclusion

A school is excluded from a field-cluster ranking if:
- It does not offer any target CIP at the bachelor's level.
- It fails the minimum completion threshold over the configured lookback window.

### 10.2 Fallback

- If field-level Scorecard outcomes are missing → use institution-level outcomes with a confidence penalty and the `using_institution_fallback` flag set to `true`.
- If grant subfield attribution is unavailable → use top-line or agency-native totals only.
- If ROR/OpenAlex identity is unresolved but the school otherwise qualifies → hold in review bucket, do not force a bad match.
- If research data are absent for an otherwise qualifying school → treat research score as missing/low-confidence, not zero.

### 10.3 Review queues

Generate review queues for:
- Ambiguous institution identity matches
- Suspiciously elite or poor rankings caused by thin data
- Unmapped target CIPs
- Newly created or renamed programs
- Schools where rank is top-25 but confidence < 0.5

---

## 11. YAML config schema

```yaml
project:
  slug: undergrad-field-rankings
  country: US
  degree_level: bachelors
  output_dir: outputs/current

years:
  candidate_universe_asof: 2024
  completions_start: 2015
  completions_end: 2024
  research_start: 2020
  research_end: 2024
  grants_start: 2020
  grants_end: 2024

institution_filters:
  include_public: true
  include_private_nonprofit: true
  include_private_forprofit: false
  include_special_focus: false
  include_branch_campuses: false
  min_total_undergrad_enrollment: 200
  min_recent_bachelors_in_target_family: 5
  allowed_country_codes: ["US"]

identity:
  canonical_internal_key: university_id
  preferred_external_research_id: ROR
  require_manual_review_for_ambiguous_matches: true
  identifier_priority:
    - IPEDS
    - COLLEGE_SCORECARD
    - ROR
    - OPENALEX
    - UEI
    - DUNS

sources:
  ipeds:
    enabled: true
    mode: bulk
    years: [2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024]
  college_scorecard:
    enabled: true
    mode: bulk_plus_api_spotcheck
    use_field_of_study: true
    credential_levels: [bachelors]
  openalex:
    enabled: true
    mode: bigquery_primary           # bigquery_primary | snapshot_fallback | api_only
    bigquery:
      project: subugoe-collaborative
      dataset: openalex
      gcp_project: your-gcp-project  # billing project for BQ queries
      max_bytes_billed: 50000000000  # 50 GB default cap per query
      export_format: parquet
      export_compression: snappy
    counting_methods:
      - primary_subfield
      - topic_deduped_subfield
  ror:
    enabled: true
    mode: api_or_registry
  usaspending:
    enabled: true
    mode: api
  nih_reporter:
    enabled: true
    mode: api_plus_bulk_if_needed
  nsf_awards:
    enabled: true
    mode: api
  common_data_set:
    enabled: false
    mode: manual_optional

field_clusters:
  - slug: molbio
    label: Molecular Biology
    cip_rules:
      include_6digit: ["26.0202", "26.0203", "26.0204"]
      include_2digit_families: ["26"]
      exclude_6digit: []
    oa_rules:
      primary_subfields: []   # populated from cip_trains_subfield if empty
      secondary_subfields: []
      adjacent_subfields: []
    fit_weights:
      primary: 1.00
      secondary: 0.60
      adjacent: 0.25
    scoring_profile: balanced

  - slug: genetics
    label: Genetics
    cip_rules:
      include_6digit: ["26.0801"]
      include_2digit_families: ["26"]
      exclude_6digit: []
    oa_rules:
      primary_subfields: []
      secondary_subfields: []
      adjacent_subfields: []
    fit_weights:
      primary: 1.00
      secondary: 0.60
      adjacent: 0.25
    scoring_profile: balanced

  - slug: statistics
    label: Statistics
    cip_rules:
      include_6digit: ["27.0501"]
      include_2digit_families: ["27"]
      exclude_6digit: []
    oa_rules:
      primary_subfields: []
      secondary_subfields: []
      adjacent_subfields: []
    fit_weights:
      primary: 1.00
      secondary: 0.60
      adjacent: 0.25
    scoring_profile: balanced

scoring_profiles:
  balanced:
    weights:
      program_availability: 0.20
      research_strength: 0.25
      student_outcomes: 0.25
      affordability_value: 0.15
      selectivity_access: 0.10
      enrichment: 0.05
  research_heavy:
    weights:
      program_availability: 0.15
      research_strength: 0.40
      student_outcomes: 0.20
      affordability_value: 0.10
      selectivity_access: 0.10
      enrichment: 0.05
  value_heavy:
    weights:
      program_availability: 0.20
      research_strength: 0.15
      student_outcomes: 0.25
      affordability_value: 0.30
      selectivity_access: 0.05
      enrichment: 0.05

feature_flags:
  allow_institution_level_outcome_fallback: true
  penalize_missing_field_outcomes: true
  compute_grant_subfield_stats_only_when_defensible: true
  produce_manual_review_queue: true
  write_run_manifest: true
  write_component_scores: true

outputs:
  write_rankings_csv: true
  write_rankings_parquet: true
  write_feature_store_parquet: true
  write_unresolved_matches_csv: true
  write_unmapped_cips_csv: true
  write_coverage_report_md: true
```

### 11.1 Config interpretation rules

- `include_6digit` defines the strongest direct educational match.
- `include_2digit_families` is for controlled expansion only — must not overwhelm the explicit list.
- `oa_rules` may be populated directly or derived from `cip_trains_subfield`. The resolved set must be stored.
- `fit_weights` apply to research-subfield aggregation only, not to completions counts.
- If `oa_rules.primary_subfields` is empty, the pipeline must auto-populate it from `cip_trains_subfield` for the cluster's CIPs.

---

## 12. Data contracts

### 12.1 Candidate universe contract

One row per `university_id`. Required fields: `university_id`, `canonical_name`, `country_code`, `state_code`, `control`, `ipeds_unitid`, `scorecard_opeid6` (if available), `bachelor_eligible` flag, enrollment context.

### 12.2 Feature store contract

One row per `university_id × field_cluster_slug`. Required fields: all feature families from §8 Stage 9, plus provenance/fallback flags, confidence sub-scores, and missingness counts.

### 12.3 Ranking output contract

One row per ranked `university_id × field_cluster_slug`. Required fields: `rank`, `canonical_name`, `field_cluster_slug`, `overall_balanced_score`, `research_opportunity_score`, `student_outcomes_score`, `value_score`, `confidence_score`, top explanatory reasons.

### 12.4 Review queue contract

One row per unresolved or suspect issue. Required fields: `issue_type`, `university_id` (if known), relevant external identifiers, human-readable explanation, suggested next action.

---

## 13. Suggested script organization

Suggested responsibilities, not mandatory filenames. Follow repo conventions.

| Responsibility              | Suggested script                          |
| --------------------------- | ----------------------------------------- |
| IPEDS universe + chars      | `load_ipeds_reference.py`                 |
| Scorecard reference         | `load_scorecard_reference.py`             |
| OpenAlex reference dims     | `load_openalex_reference.py`              |
| Identity resolution         | `resolve_university_identities.py`        |
| IPEDS completions           | `load_ipeds_completions.py`               |
| University-CIP offers       | `build_university_cip_offers.py`          |
| CIP-subfield mapping        | `load_cip_subfield_mapping.py`            |
| OpenAlex metrics extraction | `extract_openalex_metrics.py`             |
| Grant loading (USAspending) | `load_usaspending_totals.py`              |
| Grant loading (NIH)         | `load_nih_awards.py`                      |
| Grant loading (NSF)         | `load_nsf_awards.py`                      |
| Scorecard outcomes          | `load_scorecard_outcomes.py`              |
| Feature store assembly      | `build_field_cluster_features.py`         |
| Scoring and ranking         | `score_field_clusters.py`                 |
| QA and sensitivity          | `qa_coverage.py`, `qa_ranking_sensitivity.py` |

All one-off helpers must be self-contained and runnable with `uv`.

---

## 14. Acceptance criteria

### 14.1 Structural

- Uses the persisted schema from §7 — does not invent competing tables.
- Preserves `university_id` as canonical key everywhere.
- Supports config-driven field clusters.

### 14.2 Logical

- Excludes schools that do not offer the target undergraduate program.
- Handles field-level vs. institution-level outcomes as separate layers with explicit fallback flags.
- Weights research metrics by field fit, not raw publication volume.
- Treats grant totals as context, not standalone quality proxy.

### 14.3 QA

- Unresolved identity matches are surfaced, not silently forced.
- Unmapped target CIPs are surfaced.
- Missing field-outcome coverage is quantified.
- Top results pass basic plausibility check.
- Weight-profile sensitivity is reported.

### 14.4 Reproducibility

- Each run writes a manifest with source versions, years, extraction timestamps.
- Each derived feature has source/provenance metadata.
- Outputs are reproducible from config + source pulls.

### 14.5 Minimum first-pass success

For at least three configured field clusters, the pipeline produces:
- A ranked top-100 list with component scores and confidence scores.
- Explicit explanation of why each school ranks well.
- Coverage report showing where data are missing.
- Manual review file for ambiguous matches or mappings.

---

## 15. Known pain points

### 15.1 Institution identity resolution

The hardest join problem. Branch campuses, separate medical schools, multiple UEIs per system, OPEID6 rollups that don't match IPEDS units, and name variants across all systems. Budget real time for this.

### 15.2 Small-program noise

Some schools technically offer a target CIP but graduate 1–2 students per year. They should not rank well on research prestige alone. The `min_recent_bachelors_in_target_family` threshold and the program-availability score handle this.

### 15.3 Research vs. undergraduate mismatch

A school may be elite in research but weak in undergraduate access to that research. Public data can only approximate this. The confidence and enrichment layers should reflect the limitation.

### 15.4 Field-definition drift

Overlapping fields (MolBio/Genetics/Biochemistry, Statistics/Data Science/Applied Math) make boundary definition critical. The config must be explicit and the resolved CIP + subfield sets inspectable.

### 15.5 Scorecard privacy suppression

Field-of-study earnings data are suppressed for cells with fewer than 30 students. Expect 30–60% suppression rates for niche fields at smaller schools. The fallback to institution-level outcomes with confidence penalty is the mitigation.

### 15.6 BigQuery cost and data freshness

The `subugoe-collaborative.openalex` dataset on BigQuery is maintained by SUB Göttingen and updated periodically, but it may lag the live OpenAlex API by weeks. Always record the snapshot version in the run manifest. On the cost side, a careless query (e.g., `SELECT *` from `works` without year or type filters) can scan > 1 TB and cost > $6. The `maximum_bytes_billed` cap and dry-run discipline in §5.3.4 are mandatory, not optional.

---

## 16. Recommended execution order for first pass

1. Load the seeded candidate universe and backfill IPEDS identifiers/validation for that scope (Stage 1).
2. Resolve identities to Scorecard, ROR, OpenAlex (Stage 2).
3. Load CIPs and OpenAlex subfields (Stage 3).
4. Populate `cip_trains_subfield` from repo mapping source (Stage 5).
5. Load `university_cip_offers` from IPEDS completions (Stage 4).
6. Compute OpenAlex research metrics for 2020–2024 via OrionDB/BigQuery, export Parquet, load to local DB (Stage 6).
7. Load Scorecard institution-level and field-of-study outcomes (Stage 8).
8. Load top-line grants from USAspending + NIH + NSF summaries (Stage 7).
9. Build `university × field_cluster` feature store (Stage 9).
10. Produce balanced rankings and QA reports (Stages 10–11).

**Defer until first pass is stable**:
- Common Data Set enrichment
- Grant-to-subfield attribution beyond NIH institute mapping
- Manual prestige overlays
- Custom campus-life signals
