from unittest import mock

import pytest
import requests
from keboola.component.exceptions import UserException

from client import BASE_URL, RedditAdsClient, RedditAdsClientError


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, headers=None, text=""):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.headers = headers or {}
        self.text = text

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


def _client(with_token=True):
    client = RedditAdsClient("cid", "csecret", "rtoken", user_agent="test-agent/1.0")
    if with_token:
        # Skip the network refresh for _request-level tests.
        client._access_token = "tok"
        client._session.headers["Authorization"] = "bearer tok"
    return client


# ------------------------------------------------------------------ auth


def test_ensure_token_uses_basic_auth_and_user_agent():
    client = _client(with_token=False)
    resp = FakeResponse(json_body={"access_token": "abc123"})
    with mock.patch.object(client._session, "post", return_value=resp) as post:
        client._ensure_token()
    assert client._access_token == "abc123"
    assert client._session.headers["Authorization"] == "bearer abc123"
    kwargs = post.call_args.kwargs
    assert kwargs["auth"] == ("cid", "csecret")
    assert kwargs["data"] == {"grant_type": "refresh_token", "refresh_token": "rtoken"}
    assert kwargs["headers"]["User-Agent"] == "test-agent/1.0"


def test_ensure_token_is_cached():
    client = _client(with_token=False)
    resp = FakeResponse(json_body={"access_token": "abc123"})
    with mock.patch.object(client._session, "post", return_value=resp) as post:
        client._ensure_token()
        client._ensure_token()
    post.assert_called_once()  # only one refresh per run


@pytest.mark.parametrize("status", [400, 401])
def test_ensure_token_bad_credentials_raise_userexception(status):
    client = _client(with_token=False)
    with mock.patch.object(client._session, "post", return_value=FakeResponse(status_code=status)):
        with pytest.raises(UserException) as exc:
            client._ensure_token()
    assert "Reddit OAuth failed" in str(exc.value)


def test_ensure_token_missing_token_raises_userexception():
    client = _client(with_token=False)
    with mock.patch.object(client._session, "post", return_value=FakeResponse(json_body={})):
        with pytest.raises(UserException):
            client._ensure_token()


# ------------------------------------------------------------ pagination


def test_get_paginated_follows_next_url_verbatim():
    client = _client()
    next_url = f"{BASE_URL}/ad_accounts/1/campaigns?page=CURSOR2"
    pages = [
        FakeResponse(json_body={"data": [{"id": "1"}, {"id": "2"}], "pagination": {"next_url": next_url}}),
        FakeResponse(json_body={"data": [{"id": "3"}], "pagination": {}}),
    ]
    with mock.patch.object(client._session, "request", side_effect=pages) as req:
        records = list(client.get_paginated("ad_accounts/1/campaigns"))
    assert [r["id"] for r in records] == ["1", "2", "3"]
    # the second request must hit the opaque next_url verbatim, with no re-appended params
    assert req.call_args_list[1].args[1] == next_url
    assert req.call_args_list[1].kwargs["params"] == {}


def test_get_paginated_single_page():
    client = _client()
    resp = FakeResponse(json_body={"data": [{"id": "1"}]})
    with mock.patch.object(client._session, "request", return_value=resp):
        records = list(client.get_paginated("ad_accounts"))
    assert [r["id"] for r in records] == ["1"]


# --------------------------------------------------------------- reports


def test_get_report_wraps_data_reads_metrics_and_follows_next_url():
    client = _client()
    next_url = f"{BASE_URL}/ad_accounts/1/reports?page=2"
    pages = [
        # rows live under data.metrics (not a top-level list); POST creates the report
        FakeResponse(json_body={"data": {"metrics": [{"date": "2026-07-01"}]}, "pagination": {"next_url": next_url}}),
        FakeResponse(json_body={"data": {"metrics": [{"date": "2026-07-02"}]}}),
    ]
    with mock.patch.object(client._session, "request", side_effect=pages) as req:
        rows = list(client.get_report("1", {"fields": ["impressions"], "breakdowns": ["DATE"]}))
    assert [r["date"] for r in rows] == ["2026-07-01", "2026-07-02"]
    # first call is POST with the payload wrapped in {"data": ...}
    assert req.call_args_list[0].args[0] == "POST"
    assert req.call_args_list[0].kwargs["json"] == {"data": {"fields": ["impressions"], "breakdowns": ["DATE"]}}
    assert req.call_args_list[1].args[0] == "GET"  # subsequent pages are GET


def test_get_me_returns_data():
    client = _client()
    with mock.patch.object(client._session, "request", return_value=FakeResponse(json_body={"data": {"id": "t2_x"}})):
        assert client.get_me() == {"id": "t2_x"}


# ------------------------------------------------------------ retry/errors


def test_request_retries_on_429_then_succeeds():
    client = _client()
    responses = [
        FakeResponse(status_code=429, headers={"retry-after": "0"}),
        FakeResponse(json_body={"data": []}),
    ]
    with mock.patch.object(client._session, "request", side_effect=responses):
        with mock.patch("client.time.sleep") as sleep:
            resp = client._request("GET", f"{BASE_URL}/ad_accounts")
    assert resp.status_code == 200
    sleep.assert_called_once()


def test_request_401_raises_userexception():
    client = _client()
    with mock.patch.object(client._session, "request", return_value=FakeResponse(status_code=401)):
        with pytest.raises(UserException) as exc:
            client._request("GET", f"{BASE_URL}/ad_accounts")
    assert "Authentication failed" in str(exc.value)


def test_request_403_raises_userexception():
    client = _client()
    with mock.patch.object(client._session, "request", return_value=FakeResponse(status_code=403)):
        with pytest.raises(UserException) as exc:
            client._request("GET", f"{BASE_URL}/ad_accounts")
    assert "forbidden" in str(exc.value).lower()


def test_request_400_raises_userexception():
    client = _client()
    with mock.patch.object(client._session, "request", return_value=FakeResponse(status_code=400, text="bad")):
        with pytest.raises(UserException):
            client._request("GET", f"{BASE_URL}/ad_accounts")


def test_request_5xx_retried_then_userexception():
    client = _client()
    with mock.patch.object(client._session, "request", return_value=FakeResponse(status_code=500, text="boom")):
        with mock.patch("client.time.sleep") as sleep:
            with pytest.raises(UserException):
                client._request("GET", f"{BASE_URL}/ad_accounts")
    assert sleep.call_count >= 1


def test_request_502_then_success_is_retried():
    client = _client()
    responses = [FakeResponse(status_code=502), FakeResponse(json_body={"data": []})]
    with mock.patch.object(client._session, "request", side_effect=responses):
        with mock.patch("client.time.sleep"):
            resp = client._request("GET", f"{BASE_URL}/ad_accounts")
    assert resp.status_code == 200


def test_request_network_error_retried_then_raises():
    client = _client()
    with mock.patch.object(client._session, "request", side_effect=requests.ConnectionError("boom")):
        with mock.patch("client.time.sleep") as sleep:
            with pytest.raises(UserException):
                client._request("GET", f"{BASE_URL}/ad_accounts")
    assert sleep.call_count >= 1


def test_request_unexpected_status_raises_client_error():
    client = _client()
    with mock.patch.object(client._session, "request", return_value=FakeResponse(status_code=418, text="teapot")):
        with pytest.raises(RedditAdsClientError):
            client._request("GET", f"{BASE_URL}/ad_accounts")
