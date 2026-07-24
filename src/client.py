"""HTTP client for the Reddit Ads API (v3).

Handles OAuth 2.0 (refresh-token → access-token exchange), opaque ``next_url``
cursor pagination, and tenacity-driven retry/back-off for transient failures
(network errors + 429/5xx, honouring Reddit's rate-limit headers).

All network access lives here; ``component.py`` stays a thin orchestrator.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from typing import Any

import requests
import tenacity
from keboola.component.exceptions import UserException

BASE_URL = "https://ads-api.reddit.com/api/v3"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"

# Retry / back-off defaults
_MAX_RETRIES = 6
_DEFAULT_RETRY_WAIT_S = 30
_MAX_BACKOFF_S = 60

# Fallback wait when the API doesn't tell us how long to hold off: exponential
# back-off with jitter, capped.
_FALLBACK_WAIT = tenacity.wait_exponential_jitter(initial=1, max=_MAX_BACKOFF_S)

# Redact any error-body key that looks like a secret before it reaches DEBUG logs.
_SENSITIVE_KEY_HINTS = ("token", "secret", "password", "authorization", "assertion", "client_id")


def _is_transient_status(status: int) -> bool:
    """Retryable HTTP statuses: rate-limit (429) and any server-side 5xx.

    Kept deliberately broad (``>= 500`` rather than an allow-list) so a new 5xx
    variant the vendor introduces is retried, not surfaced as an internal error.
    """
    return status == 429 or status >= 500


def _redact(value: Any) -> Any:
    """Recursively mask values under secret-looking keys, for safe DEBUG logging."""
    if isinstance(value, dict):
        return {
            k: ("***" if any(h in k.lower() for h in _SENSITIVE_KEY_HINTS) else _redact(v)) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


class RedditAdsClientError(Exception):
    """Unexpected API failure (bubbles up as exit code 2)."""


class _TransientError(Exception):
    """Internal marker for a retryable failure (network error or 429/5xx).

    Carries the explicit back-off ``wait`` Reddit asked for (via headers) when
    available, plus the ``status`` or the underlying network ``cause`` so the
    retry-exhausted message can be specific.
    """

    def __init__(self, *, wait: float | None = None, status: int | None = None, cause: Exception | None = None) -> None:
        self.wait = wait
        self.status = status
        self.cause = cause
        super().__init__(f"transient Reddit Ads API failure (status={status})")


class RedditAdsClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        *,
        user_agent: str,
        request_timeout: int = 60,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._user_agent = user_agent
        self._timeout = request_timeout
        self._session = requests.Session()
        # Reddit rejects requests without a real User-Agent — set it on every call.
        self._session.headers["User-Agent"] = user_agent
        self._access_token: str | None = None

    # -------------------------------------------------------------------- auth

    def _ensure_token(self) -> None:
        """Obtain an access token once per run via the refresh-token grant.

        Reddit access tokens live at least an hour, so a single refresh at the
        start of a run is sufficient. The exchange goes through the same tenacity
        retry/back-off as every API call, so a transient 429/5xx (or a network
        blip) on the token endpoint is retried rather than surfaced as an internal
        error (exit 2).
        """
        if self._access_token is not None:
            return
        retryer = tenacity.Retrying(
            retry=tenacity.retry_if_exception_type(_TransientError),
            stop=tenacity.stop_after_attempt(_MAX_RETRIES),
            wait=self._retry_wait,
            sleep=time.sleep,
            before_sleep=self._log_retry,
            reraise=False,
        )
        try:
            token = retryer(self._attempt_token)
        except tenacity.RetryError as err:
            transient = err.last_attempt.exception()
            raise self._exhausted_error(TOKEN_URL, transient) from transient
        self._access_token = token
        self._session.headers["Authorization"] = f"bearer {token}"

    def _attempt_token(self) -> str:
        """Perform a single token exchange (HTTP Basic auth, as Reddit requires).

        Raises ``_TransientError`` on a retryable failure and ``UserException`` on
        a credential problem; returns the access token on success.
        """
        try:
            resp = self._session.post(
                TOKEN_URL,
                auth=(self._client_id, self._client_secret),
                data={"grant_type": "refresh_token", "refresh_token": self._refresh_token},
                headers={"User-Agent": self._user_agent},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise _TransientError(cause=exc) from exc

        if _is_transient_status(resp.status_code):
            raise _TransientError(status=resp.status_code, wait=self._transient_wait(resp))
        if resp.status_code in (400, 401):
            raise UserException(
                f"Reddit OAuth failed (HTTP {resp.status_code}). Check the Client ID, Client Secret and "
                f"Refresh Token — the refresh token may have been revoked or the app credentials are wrong."
            )
        if not resp.ok:
            raise RedditAdsClientError(f"Unexpected error obtaining an access token (HTTP {resp.status_code}).")

        token = (resp.json() or {}).get("access_token")
        if not token:
            raise UserException("Reddit OAuth response did not contain an access token. Check your credentials.")
        return token

    # -------------------------------------------------------------------- core

    def _request(
        self, method: str, url: str, params: dict[str, Any] | None = None, json: Any = None
    ) -> requests.Response:
        self._ensure_token()
        retryer = tenacity.Retrying(
            retry=tenacity.retry_if_exception_type(_TransientError),
            stop=tenacity.stop_after_attempt(_MAX_RETRIES),
            wait=self._retry_wait,
            sleep=time.sleep,
            before_sleep=self._log_retry,
            reraise=False,
        )
        try:
            return retryer(self._attempt_request, method, url, params, json)
        except tenacity.RetryError as err:
            transient = err.last_attempt.exception()
            raise self._exhausted_error(url, transient) from transient

    def _attempt_request(self, method: str, url: str, params: dict[str, Any] | None, json: Any) -> requests.Response:
        """Perform a single request. Raises ``_TransientError`` on retryable failures,
        ``UserException`` on user-fixable ones, and returns the response on success."""
        try:
            resp = self._session.request(method, url, params=params, json=json, timeout=self._timeout)
        except requests.RequestException as exc:
            raise _TransientError(cause=exc) from exc

        if _is_transient_status(resp.status_code):
            raise _TransientError(status=resp.status_code, wait=self._transient_wait(resp))
        if resp.status_code == 401:
            raise UserException(
                "Authentication failed (401). The Reddit access token was rejected — check the app "
                "credentials and that the authorizing account has access to this ad account."
            )
        if resp.status_code == 403:
            logging.debug("403 response body for %s: %s", url, self._error_detail(resp))
            raise UserException(
                f"Access forbidden (403) for '{url}'. The authorizing account may lack permission for "
                f"this ad account or the required scope (adsread)."
            )
        # Any remaining 4xx is user-fixable (bad parameter, missing/unsupported resource, …).
        # Treat the whole range so a status we didn't special-case never falls through to an
        # opaque internal error (exit 2).
        if 400 <= resp.status_code < 500:
            logging.debug("HTTP %s response body for %s: %s", resp.status_code, url, self._error_detail(resp))
            raise UserException(
                f"Request to '{url}' was rejected (HTTP {resp.status_code}). This usually means a "
                f"configuration problem (invalid parameter, unsupported value or missing resource)."
            )
        if not resp.ok:
            logging.debug("HTTP %s response body for %s: %s", resp.status_code, url, self._error_detail(resp))
            raise RedditAdsClientError(f"{method} {url} failed with HTTP {resp.status_code}.")
        return resp

    @staticmethod
    def _retry_wait(retry_state: tenacity.RetryCallState) -> float:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        explicit = getattr(exc, "wait", None)
        if explicit is not None:
            return explicit
        return _FALLBACK_WAIT(retry_state)

    @staticmethod
    def _log_retry(retry_state: tenacity.RetryCallState) -> None:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        label = getattr(exc, "status", None) or type(getattr(exc, "cause", exc)).__name__
        logging.warning("Transient Reddit Ads API failure (%s); retry %d.", label, retry_state.attempt_number)

    def _transient_wait(self, resp: requests.Response) -> float | None:
        # Reddit returns rate-limit info in headers; honour Retry-After / reset when present.
        retry_after = resp.headers.get("retry-after") or resp.headers.get("x-ratelimit-reset")
        if retry_after:
            try:
                return max(1.0, float(retry_after))
            except ValueError:
                return _DEFAULT_RETRY_WAIT_S
        if resp.status_code == 429:
            return _DEFAULT_RETRY_WAIT_S
        return None  # 5xx → exponential fallback

    @staticmethod
    def _exhausted_error(url: str, transient: BaseException | None) -> UserException:
        cause = getattr(transient, "cause", None)
        if cause is not None:
            return UserException(
                f"Could not reach the Reddit Ads API for '{url}' after {_MAX_RETRIES} attempts "
                f"({type(cause).__name__}). This is usually a temporary network or service issue — "
                f"please try again later."
            )
        status = getattr(transient, "status", None)
        return UserException(
            f"The Reddit Ads API kept returning transient errors (last HTTP {status}) for '{url}' after "
            f"{_MAX_RETRIES} attempts. This is usually a temporary rate-limit or service issue — please try again later."
        )

    @staticmethod
    def _error_detail(resp: requests.Response) -> str:
        try:
            body = resp.json()
            if isinstance(body, dict):
                errors = body.get("errors") or body.get("error")
                if errors:
                    return json.dumps(_redact(errors))[:500]
        except ValueError:
            pass
        return resp.text[:500]

    # ------------------------------------------------------------- pagination

    def get_paginated(self, path: str, params: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        """Yield every record across pages of ``path``.

        Reddit returns a ``pagination`` object with an opaque ``next_url`` that
        must be followed **verbatim** (its query params are pre-baked; we never
        reconstruct them). The first request uses ``path`` + ``params``; every
        subsequent request uses ``next_url`` as-is.
        """
        url: str | None = f"{BASE_URL}/{path.lstrip('/')}"
        request_params = dict(params or {})
        while url:
            body = self._request("GET", url, params=request_params).json()
            yield from body.get("data") or []
            url = ((body.get("pagination") or {}).get("next_url")) or None
            # next_url carries its own query string; don't re-append the initial params.
            request_params = {}

    def get_me(self) -> dict[str, Any]:
        """The authenticated user (used by Test Connection)."""
        return self._request("GET", f"{BASE_URL}/me").json().get("data") or {}

    # ------------------------------------------------------------- reporting

    def get_report(self, account_id: str, report_data: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Run the reports endpoint and yield report rows across all pages.

        The report is created with a POST whose params are wrapped in a ``data``
        object (verified live): ``{"data": {"fields": [...], "starts_at": ...,
        "ends_at": ..., "breakdowns": [...]}}``. The response carries the rows under
        ``data.metrics`` (not a top-level list) plus a ``pagination`` object whose
        ``next_url`` is followed verbatim via GET for further pages.
        """
        url: str | None = f"{BASE_URL}/ad_accounts/{account_id}/reports"
        first = True
        while url:
            resp = self._request("POST", url, json={"data": report_data}) if first else self._request("GET", url)
            payload = resp.json()
            data = payload.get("data") or {}
            yield from (data.get("metrics") if isinstance(data, dict) else data) or []
            url = ((payload.get("pagination") or {}).get("next_url")) or None
            first = False
