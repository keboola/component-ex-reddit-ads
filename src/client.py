"""HTTP client for the Reddit Ads API (v3).

Handles OAuth 2.0 (refresh-token → access-token exchange), opaque ``next_url``
cursor pagination, and tenacity-driven retry/back-off for transient failures
(network errors + 429/5xx, honouring Reddit's rate-limit headers).

All network access lives here; ``component.py`` stays a thin orchestrator.
"""

from __future__ import annotations

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

# Statuses worth retrying vs. surfacing as a user-fixable error.
_TRANSIENT_STATUS = {429, 500, 502, 503, 504}
_USER_ERROR_STATUS = {400, 404, 422}

# Fallback wait when the API doesn't tell us how long to hold off: exponential
# back-off with jitter, capped.
_FALLBACK_WAIT = tenacity.wait_exponential_jitter(initial=1, max=_MAX_BACKOFF_S)


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
        start of a run is sufficient. The token exchange uses HTTP Basic auth
        (client_id:client_secret) as Reddit requires.
        """
        if self._access_token is not None:
            return
        try:
            resp = self._session.post(
                TOKEN_URL,
                auth=(self._client_id, self._client_secret),
                data={"grant_type": "refresh_token", "refresh_token": self._refresh_token},
                headers={"User-Agent": self._user_agent},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise UserException(
                f"Could not reach Reddit's OAuth token endpoint ({type(exc).__name__}). "
                f"This is usually a temporary network issue — please try again later."
            ) from exc

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
        self._access_token = token
        self._session.headers["Authorization"] = f"bearer {token}"

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

        if resp.status_code in _TRANSIENT_STATUS:
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
        if resp.status_code in _USER_ERROR_STATUS:
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
                    import json as _json

                    return _json.dumps(errors)[:500]
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

    # --------------------------------------------------------------- entities

    def list_objects(self, account_id: str, object_path: str) -> Iterator[dict[str, Any]]:
        """List an account-scoped entity collection (campaigns, ad_groups, …)."""
        return self.get_paginated(f"ad_accounts/{account_id}/{object_path}")

    def list_ad_accounts(self) -> list[dict[str, Any]]:
        """List the ad accounts the authorized user can access (used by sync actions)."""
        return list(self.get_paginated("ad_accounts"))

    def list_time_zones(self) -> list[dict[str, Any]]:
        """Reference lookup of supported report time zones (also backs a dropdown)."""
        return list(self.get_paginated("time_zones"))

    # ------------------------------------------------------------- reporting

    def get_report(self, account_id: str, body: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Run the reports endpoint and yield report rows across all pages.

        ``body`` is the request payload (level, metrics, breakdowns, date range,
        time zone, granularity). The exact param names are isolated to the caller
        (``component.py``) so they are easy to correct once verified against a
        live call / the official Postman collection.
        """
        url: str | None = f"{BASE_URL}/ad_accounts/{account_id}/reports"
        first = True
        while url:
            # The report is created by a POST; subsequent pages follow next_url via GET.
            method = "POST" if first else "GET"
            resp = self._request(method, url, json=body if first else None)
            payload = resp.json()
            yield from payload.get("data") or []
            url = ((payload.get("pagination") or {}).get("next_url")) or None
            first = False
