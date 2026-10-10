"""BigQuery request policy for the OpenAlex extractor (PR #55 review).

Two properties matter here and neither is covered by the loader's own tests:
creating a query job must never be replayed, and the timeout the operator
passes must actually reach the adapter.
"""

from __future__ import annotations

from typing import Any

from extract_openalex_metrics import ExtractOpenAlexMetricsLoader, submit_query_job
from shared.http import FetchCall, RecordedJsonSource


def _config(call: FetchCall) -> dict[str, Any]:
    """The job configuration of a recorded call, asserted present."""
    assert call.payload is not None, f"expected a JSON body on {call.url}"
    return call.payload["configuration"]


def _submit(*, dry_run: bool) -> RecordedJsonSource:
    source = RecordedJsonSource(lambda _call: {"jobReference": {"jobId": "j", "location": "US"}})
    submit_query_job(
        source=source,
        token="token",
        project_id="proj",
        query="SELECT 1",
        max_bytes_billed=1,
        dry_run=dry_run,
    )
    return source


def test_a_real_query_job_insert_is_never_retried() -> None:
    """A replayed jobs.insert creates a second job that bills a full scan."""
    call: FetchCall = _submit(dry_run=False).calls[0]
    assert call.retry is False
    assert call.method == "POST"
    assert _config(call)["dryRun"] is False


def test_a_dry_run_job_keeps_the_normal_retry_policy() -> None:
    """Dry runs are free and persist no job, so replaying one costs nothing."""
    call: FetchCall = _submit(dry_run=True).calls[0]
    assert call.retry is True
    assert _config(call)["dryRun"] is True


def test_bigquery_requests_do_not_pin_a_literal_timeout() -> None:
    """The timeout must come from --request-timeout, not a hard-coded 180."""
    for dry_run in (True, False):
        assert _submit(dry_run=dry_run).calls[0].timeout is None


def test_request_timeout_flag_reaches_the_adapter() -> None:
    loader = ExtractOpenAlexMetricsLoader()
    default: Any = loader.build_json_source(loader._parse_args([]))
    assert default.timeout == 180
    overridden: Any = loader.build_json_source(loader._parse_args(["--request-timeout", "45"]))
    assert overridden.timeout == 45
