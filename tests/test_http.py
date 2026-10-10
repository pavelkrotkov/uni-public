from __future__ import annotations

import io
import json
import urllib.error
from email.message import Message
from types import TracebackType
from typing import Any
from unittest.mock import patch

import pytest
from shared.http import (
    FetchCall,
    HttpJsonSource,
    JsonSourceError,
    JsonSourceHTTPError,
    RecordedJsonSource,
    is_retryable_status,
    redact_url,
)


class _FakeResponse(io.BytesIO):
    """Minimal stand-in for the object urlopen yields as a context manager."""

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        return None


def _ok(payload: Any) -> _FakeResponse:
    return _FakeResponse(json.dumps(payload).encode("utf-8"))


def _http_error(status: int, body: str = "") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://example.test/x",
        code=status,
        msg="boom",
        hdrs=Message(),
        fp=io.BytesIO(body.encode("utf-8")),
    )


def _source(**kwargs: Any) -> HttpJsonSource:
    kwargs.setdefault("sleep", lambda _seconds: None)
    return HttpJsonSource(**kwargs)


# --------------------------------------------------------------------------
# retry policy
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(429, True), (500, True), (503, True), (400, False), (401, False), (404, False)],
)
def test_is_retryable_status(status: int, retryable: bool) -> None:
    assert is_retryable_status(status) is retryable


def test_429_is_retried_then_succeeds() -> None:
    responses = [_http_error(429), _ok({"ok": True})]
    with patch("urllib.request.urlopen", side_effect=responses) as urlopen:
        assert _source().fetch("https://example.test/x") == {"ok": True}
    assert urlopen.call_count == 2


def test_5xx_is_retried_then_succeeds() -> None:
    responses = [_http_error(503), _http_error(500), _ok({"ok": True})]
    with patch("urllib.request.urlopen", side_effect=responses) as urlopen:
        assert _source().fetch("https://example.test/x") == {"ok": True}
    assert urlopen.call_count == 3


def test_404_raises_immediately_without_retrying() -> None:
    """The IPEDS year probe depends on this: a missing year must not cost 5 attempts."""
    with (
        patch("urllib.request.urlopen", side_effect=_http_error(404)) as urlopen,
        pytest.raises(JsonSourceHTTPError) as excinfo,
    ):
        _source().fetch("https://example.test/x")
    assert urlopen.call_count == 1
    assert excinfo.value.status == 404


def test_401_raises_immediately_and_reports_the_status() -> None:
    """A bad SCORECARD_API_KEY should surface as 401, not as a generic failure."""
    with (
        patch("urllib.request.urlopen", side_effect=_http_error(401, "bad api key")),
        pytest.raises(JsonSourceHTTPError) as excinfo,
    ):
        _source().fetch("https://example.test/x")
    assert excinfo.value.status == 401
    assert excinfo.value.body == "bad api key"
    assert "401" in str(excinfo.value)


def test_exhausted_attempts_raise_http_error_carrying_the_last_status() -> None:
    with (
        patch("urllib.request.urlopen", side_effect=_http_error(503)) as urlopen,
        pytest.raises(JsonSourceHTTPError) as excinfo,
    ):
        _source(max_attempts=3).fetch("https://example.test/x")
    assert urlopen.call_count == 3
    assert excinfo.value.status == 503


def test_transport_errors_are_retried_then_raise_json_source_error() -> None:
    with (
        patch("urllib.request.urlopen", side_effect=OSError("connection reset")) as urlopen,
        pytest.raises(JsonSourceError) as excinfo,
    ):
        _source(max_attempts=4).fetch("https://example.test/x")
    assert urlopen.call_count == 4
    assert not isinstance(excinfo.value, JsonSourceHTTPError)


def test_malformed_json_is_retried() -> None:
    responses = [_FakeResponse(b"not json"), _ok({"ok": True})]
    with patch("urllib.request.urlopen", side_effect=responses) as urlopen:
        assert _source().fetch("https://example.test/x") == {"ok": True}
    assert urlopen.call_count == 2


def test_errors_subclass_runtime_error() -> None:
    """Callers that already catch RuntimeError around a fetch keep working."""
    assert issubclass(JsonSourceError, RuntimeError)
    assert issubclass(JsonSourceHTTPError, JsonSourceError)


def test_backoff_doubles_between_attempts() -> None:
    delays: list[float] = []
    source = HttpJsonSource(max_attempts=4, initial_delay=1.0, sleep=delays.append)
    with (
        patch("urllib.request.urlopen", side_effect=_http_error(500)),
        pytest.raises(JsonSourceHTTPError),
    ):
        source.fetch("https://example.test/x")
    assert delays == [1.0, 2.0, 4.0]


def test_max_attempts_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        HttpJsonSource(max_attempts=0)


# --------------------------------------------------------------------------
# request shaping
# --------------------------------------------------------------------------


def test_params_are_urlencoded_onto_the_query_string() -> None:
    with patch("urllib.request.urlopen", return_value=_ok({})) as urlopen:
        _source().fetch("https://example.test/x", params={"a": 1, "b": "two"})
    assert urlopen.call_args.args[0].full_url == "https://example.test/x?a=1&b=two"


def test_params_append_to_a_url_that_already_has_a_query() -> None:
    with patch("urllib.request.urlopen", return_value=_ok({})) as urlopen:
        _source().fetch("https://example.test/x?year=2024", params={"a": 1})
    assert urlopen.call_args.args[0].full_url == "https://example.test/x?year=2024&a=1"


def test_payload_is_sent_as_json_and_implies_post() -> None:
    with patch("urllib.request.urlopen", return_value=_ok({})) as urlopen:
        _source().fetch("https://example.test/x", payload={"criteria": {"a": 1}})
    request = urlopen.call_args.args[0]
    assert request.get_method() == "POST"
    assert json.loads(request.data) == {"criteria": {"a": 1}}
    assert request.headers["Content-type"] == "application/json"


def test_explicit_method_overrides_the_payload_default() -> None:
    with patch("urllib.request.urlopen", return_value=_ok({})) as urlopen:
        _source().fetch("https://example.test/x", payload={"a": 1}, method="PUT")
    assert urlopen.call_args.args[0].get_method() == "PUT"


def test_per_request_headers_merge_over_the_source_headers() -> None:
    source = _source(headers={"User-Agent": "univ/1.0"})
    with patch("urllib.request.urlopen", return_value=_ok({})) as urlopen:
        source.fetch("https://example.test/x", headers={"Authorization": "Bearer t"})
    headers = urlopen.call_args.args[0].headers
    assert headers["User-agent"] == "univ/1.0"
    assert headers["Authorization"] == "Bearer t"


def test_per_request_timeout_overrides_the_source_default() -> None:
    with patch("urllib.request.urlopen", return_value=_ok({})) as urlopen:
        _source(timeout=30).fetch("https://example.test/x", timeout=180)
    assert urlopen.call_args.kwargs["timeout"] == 180


def test_a_bare_list_response_is_returned_as_is() -> None:
    """SAM.gov answers with a list, not an object."""
    with patch("urllib.request.urlopen", return_value=_ok([{"ueiSAM": "ABC"}])):
        assert _source().fetch("https://example.test/x") == [{"ueiSAM": "ABC"}]


# --------------------------------------------------------------------------
# RecordedJsonSource
# --------------------------------------------------------------------------


def test_recorded_source_serves_a_sequence_in_order_and_records_calls() -> None:
    source = RecordedJsonSource([{"n": 1}, {"n": 2}])
    assert source.fetch("https://a.test", params={"p": 1}) == {"n": 1}
    assert source.fetch("https://b.test", payload={"q": 2}) == {"n": 2}
    assert source.urls == ["https://a.test?p=1", "https://b.test"]
    assert source.calls[1].payload == {"q": 2}


def test_recorded_source_supports_a_responder_callable() -> None:
    def responder(call: FetchCall) -> dict[str, Any]:
        return {"echo": call.url}

    source = RecordedJsonSource(responder)
    assert source.fetch("https://a.test") == {"echo": "https://a.test"}
    assert source.fetch("https://b.test") == {"echo": "https://b.test"}


def test_recorded_source_raises_queued_exceptions() -> None:
    source = RecordedJsonSource([JsonSourceHTTPError("nope", status=404)])
    with pytest.raises(JsonSourceHTTPError):
        source.fetch("https://a.test")


def test_recorded_source_fails_loudly_when_it_runs_out_of_responses() -> None:
    source = RecordedJsonSource([{"n": 1}])
    source.fetch("https://a.test")
    with pytest.raises(AssertionError, match="ran out of responses"):
        source.fetch("https://b.test")


# --------------------------------------------------------------------------
# credential redaction (PR #55 review)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["api_key", "API_KEY", "apikey", "key", "token", "access_token", "secret"]
)
def test_redact_url_masks_every_sensitive_parameter_name(name: str) -> None:
    assert redact_url(f"https://x.test/s?{name}=hunter2&id=7") == (
        f"https://x.test/s?{name}=REDACTED&id=7"
    )


def test_redact_url_leaves_urls_without_credentials_alone() -> None:
    assert redact_url("https://x.test/s?id=7&per_page=10") == "https://x.test/s?id=7&per_page=10"
    assert redact_url("https://x.test/s") == "https://x.test/s"


def test_api_key_is_not_in_the_message_of_an_immediate_http_error() -> None:
    """A Scorecard 401 is exactly when the key is present and wrong."""
    with (
        patch("urllib.request.urlopen", side_effect=_http_error(401)),
        pytest.raises(JsonSourceHTTPError) as excinfo,
    ):
        _source().fetch("https://api.test/schools", params={"api_key": "SECRETKEY", "id": "1"})
    message = str(excinfo.value)
    assert "SECRETKEY" not in message
    assert "api_key=REDACTED" in message
    assert "id=1" in message


def test_api_key_is_not_in_the_message_after_attempts_are_exhausted() -> None:
    with (
        patch("urllib.request.urlopen", side_effect=_http_error(503)),
        pytest.raises(JsonSourceHTTPError) as excinfo,
    ):
        _source(max_attempts=2).fetch("https://api.test/s", params={"api_key": "SECRETKEY"})
    assert "SECRETKEY" not in str(excinfo.value)


def test_api_key_is_not_in_the_message_of_a_transport_failure() -> None:
    with (
        patch("urllib.request.urlopen", side_effect=OSError("reset")),
        pytest.raises(JsonSourceError) as excinfo,
    ):
        _source(max_attempts=1).fetch("https://api.test/s", params={"api_key": "SECRETKEY"})
    assert "SECRETKEY" not in str(excinfo.value)


def test_the_request_still_carries_the_real_key() -> None:
    """Redaction is for messages only — it must not alter what is sent."""
    with patch("urllib.request.urlopen", return_value=_ok({})) as urlopen:
        _source().fetch("https://api.test/s", params={"api_key": "SECRETKEY"})
    assert "api_key=SECRETKEY" in urlopen.call_args.args[0].full_url


def test_recorded_source_urls_are_redacted() -> None:
    source = RecordedJsonSource([{}])
    source.fetch("https://api.test/s", params={"api_key": "SECRETKEY"})
    assert source.urls == ["https://api.test/s?api_key=REDACTED"]
    assert source.calls[0].full_url.endswith("api_key=SECRETKEY")


# --------------------------------------------------------------------------
# retry=False for non-idempotent requests (PR #55 review)
# --------------------------------------------------------------------------


def test_retry_false_makes_exactly_one_attempt_on_a_retryable_status() -> None:
    """A replayed BigQuery jobs.insert bills a second full scan."""
    with (
        patch("urllib.request.urlopen", side_effect=_http_error(503)) as urlopen,
        pytest.raises(JsonSourceHTTPError),
    ):
        _source(max_attempts=5).fetch("https://api.test/jobs", payload={"a": 1}, retry=False)
    assert urlopen.call_count == 1


def test_retry_false_makes_exactly_one_attempt_on_a_transport_failure() -> None:
    with (
        patch("urllib.request.urlopen", side_effect=OSError("reset")) as urlopen,
        pytest.raises(JsonSourceError),
    ):
        _source(max_attempts=5).fetch("https://api.test/jobs", payload={"a": 1}, retry=False)
    assert urlopen.call_count == 1


def test_retry_defaults_to_true() -> None:
    with patch("urllib.request.urlopen", side_effect=[_http_error(503), _ok({"ok": True})]):
        assert _source().fetch("https://api.test/x") == {"ok": True}


def test_recorded_source_records_the_retry_flag() -> None:
    source = RecordedJsonSource([{}, {}])
    source.fetch("https://api.test/jobs", retry=False)
    source.fetch("https://api.test/queries")
    assert [call.retry for call in source.calls] == [False, True]
