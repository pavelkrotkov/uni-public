"""JSON-over-HTTP seam for the repo's data loaders.

Every loader that talks to an external API goes through a ``JsonSource``
instead of calling ``urllib`` directly. Two adapters implement it:

* ``HttpJsonSource`` — the production adapter. Owns headers, timeout, attempt
  count, backoff, and the single retry policy described below.
* ``RecordedJsonSource`` — the test adapter. Serves queued or computed
  payloads and records every call, so a loader's ``load()`` can be exercised
  end-to-end without a network.

Retry policy (one policy, applied everywhere):

* ``429`` and ``5xx`` are retried with exponential backoff.
* Other ``4xx`` responses are raised immediately — retrying a 401 from a bad
  API key or a 404 for a year that does not exist only wastes attempts and
  buries the real status behind a generic failure message.
* Transport errors and malformed JSON are retried.
* When attempts are exhausted the last error is re-raised as a
  ``JsonSourceError`` (or ``JsonSourceHTTPError`` when the last failure carried
  a status).

Both error types subclass ``RuntimeError`` so callers that already catch
``RuntimeError`` around a fetch keep working.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

DEFAULT_TIMEOUT = 60
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_INITIAL_DELAY = 1.0
RETRYABLE_STATUSES = frozenset({429})

# Query parameters whose values must never reach a log or an exception message.
# Scorecard and OpenAlex both pass their API key on the query string.
SENSITIVE_PARAM_NAMES = frozenset(
    {"api_key", "apikey", "key", "token", "access_token", "password", "secret"}
)
REDACTED = "REDACTED"


def redact_url(url: str) -> str:
    """Return ``url`` with the value of any sensitive query parameter masked.

    Error messages quote the requested URL, and a Scorecard 401 is exactly the
    case where the key is both present and wrong — so without this the key
    lands in terminal and CI logs.
    """
    parsed = urllib.parse.urlsplit(url)
    if not parsed.query:
        return url
    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if not any(name.lower() in SENSITIVE_PARAM_NAMES for name, _ in pairs):
        return url
    masked = [
        (name, REDACTED if name.lower() in SENSITIVE_PARAM_NAMES else value)
        for name, value in pairs
    ]
    return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(masked)))


class JsonSourceError(RuntimeError):
    """A JSON request failed and will not be retried further."""


class JsonSourceHTTPError(JsonSourceError):
    """A JSON request failed with an HTTP status.

    ``status`` is the response code. ``body`` is the response body when the
    adapter could read it, which is where APIs like BigQuery put the actual
    explanation.
    """

    def __init__(self, message: str, *, status: int, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def is_retryable_status(status: int) -> bool:
    """True when ``status`` should be retried rather than raised immediately."""
    return status >= 500 or status in RETRYABLE_STATUSES


@dataclass
class FetchCall:
    """One recorded request. Used by ``RecordedJsonSource`` in tests."""

    url: str
    params: Mapping[str, Any] | None = None
    payload: Any | None = None
    headers: Mapping[str, str] | None = None
    method: str | None = None
    timeout: float | None = None
    retry: bool = True

    @property
    def full_url(self) -> str:
        """URL with ``params`` applied, matching what the HTTP adapter would request."""
        if not self.params:
            return self.url
        separator = "&" if "?" in self.url else "?"
        return f"{self.url}{separator}{urllib.parse.urlencode(self.params)}"

    @property
    def safe_url(self) -> str:
        """``full_url`` with API keys masked. Use this anywhere a URL is logged."""
        return redact_url(self.full_url)


class JsonSource(Protocol):
    """Fetches a JSON document. The seam every loader depends on."""

    def fetch(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        payload: Any | None = None,
        headers: Mapping[str, str] | None = None,
        method: str | None = None,
        timeout: float | None = None,
        retry: bool = True,
    ) -> Any:
        """Return the decoded JSON body.

        ``params`` are urlencoded onto the query string. ``payload`` is sent as
        a JSON body and implies POST unless ``method`` says otherwise. The
        return type is ``Any`` because not every API returns an object —
        SAM.gov returns a bare list.

        Set ``retry=False`` for a request that is not safe to repeat. Creating
        a BigQuery query job is the case in this repo: a replayed insert bills
        a second full scan. Idempotency is a property of the request, so it is
        declared here rather than configured on the adapter.
        """
        ...


class HttpJsonSource:
    """Production ``JsonSource``: urllib plus the shared retry policy.

    Stateless per request, so a single instance is safe to share across
    threads (``load_ipeds_completions`` fans out over a thread pool).
    """

    def __init__(
        self,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        initial_delay: float = DEFAULT_INITIAL_DELAY,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.initial_delay = initial_delay
        self._sleep = sleep

    def fetch(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        payload: Any | None = None,
        headers: Mapping[str, str] | None = None,
        method: str | None = None,
        timeout: float | None = None,
        retry: bool = True,
    ) -> Any:
        request_url = url
        if params:
            separator = "&" if "?" in url else "?"
            request_url = f"{url}{separator}{urllib.parse.urlencode(params)}"

        body = None if payload is None else json.dumps(payload).encode("utf-8")
        merged_headers = dict(self.headers)
        if body is not None:
            merged_headers.setdefault("Content-Type", "application/json")
        if headers:
            merged_headers.update(headers)

        effective_timeout = self.timeout if timeout is None else timeout
        attempts = self.max_attempts if retry else 1
        safe_url = redact_url(request_url)
        delay = self.initial_delay
        last_error: Exception | None = None
        last_status: int | None = None
        last_body = ""

        for attempt in range(attempts):
            request = urllib.request.Request(
                request_url, data=body, headers=merged_headers, method=method
            )
            try:
                with urllib.request.urlopen(request, timeout=effective_timeout) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                error_body = _read_error_body(exc)
                if not is_retryable_status(exc.code):
                    raise JsonSourceHTTPError(
                        f"{safe_url} failed with HTTP {exc.code}: {error_body or exc.reason}",
                        status=exc.code,
                        body=error_body,
                    ) from exc
                last_error, last_status, last_body = exc, exc.code, error_body
            except Exception as exc:
                # Transport failures and malformed JSON: retry, then give up.
                last_error, last_status, last_body = exc, None, ""

            if attempt == attempts - 1:
                break
            self._sleep(delay)
            delay *= 2

        message = f"{safe_url} failed after {attempts} attempt(s): {last_error}"
        if last_status is not None:
            raise JsonSourceHTTPError(message, status=last_status, body=last_body) from last_error
        raise JsonSourceError(message) from last_error


def _read_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


Responder = Callable[[FetchCall], Any]


def _queue_responder(responses: Sequence[Any]) -> Responder:
    """Serve ``responses`` in order, failing loudly when they run out."""
    queue = list(responses)
    served = 0

    def respond(call: FetchCall) -> Any:
        nonlocal served
        served += 1
        if not queue:
            raise AssertionError(
                f"RecordedJsonSource ran out of responses at call {served}: {call.safe_url}"
            )
        return queue.pop(0)

    return respond


class RecordedJsonSource:
    """Test ``JsonSource``: serves canned payloads and records every call.

    ``responses`` is either a callable taking the :class:`FetchCall` and
    returning the payload, or a sequence consumed in order. In both cases an
    ``Exception`` instance is raised rather than returned, which is how tests
    drive the failure paths.

    Thread-safe, because ``load_ipeds_completions`` fetches from a pool.
    """

    def __init__(self, responses: Responder | Sequence[Any]) -> None:
        # isinstance on Sequence rather than callable() — a responder is not a
        # Sequence, and this form narrows cleanly for the type checker.
        if isinstance(responses, Sequence):
            self._responder: Responder = _queue_responder(responses)
        else:
            self._responder = responses
        self.calls: list[FetchCall] = []
        self._lock = threading.Lock()

    def fetch(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        payload: Any | None = None,
        headers: Mapping[str, str] | None = None,
        method: str | None = None,
        timeout: float | None = None,
        retry: bool = True,
    ) -> Any:
        call = FetchCall(
            url=url,
            params=dict(params) if params is not None else None,
            payload=payload,
            headers=dict(headers) if headers is not None else None,
            method=method,
            timeout=timeout,
            retry=retry,
        )
        with self._lock:
            self.calls.append(call)
            result = self._responder(call)
        if isinstance(result, Exception):
            raise result
        return result

    @property
    def urls(self) -> list[str]:
        """Every requested URL, params applied and keys masked, in call order."""
        return [call.safe_url for call in self.calls]
