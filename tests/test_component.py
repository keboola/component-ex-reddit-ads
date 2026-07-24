from datetime import date, datetime

import pytest
from freezegun import freeze_time
from keboola.component.exceptions import UserException

from component import _EMPTY_PK_PLACEHOLDER, _REGISTRY, Component
from configuration import Configuration, ObjectType, ReportConfig

_CREDS = {"#client_id": "cid", "#client_secret": "csec", "#refresh_token": "rt"}


def _bare_component() -> Component:
    # Build a Component without running __init__ (which needs the KBC datadir + creds);
    # the helpers under test are pure and don't touch instance state beyond staticmethods.
    return object.__new__(Component)


# ------------------------------------------------------------- flattening


def test_flatten_record_jsonifies_nested():
    row = {"id": "1", "name": "x", "targeting": {"geo": ["US"]}, "tags": [1, 2]}
    flat = Component._flatten_record(row)
    assert flat["id"] == "1"
    assert flat["targeting"] == '{"geo": ["US"]}'
    assert flat["tags"] == "[1, 2]"


# ------------------------------------------------------------ incremental


def test_is_modified_since():
    since = datetime.fromisoformat("2026-07-01T00:00:00+00:00")
    assert Component._is_modified_since({"modified_at": "2026-07-05T00:00:00Z"}, "modified_at", since) is True
    assert Component._is_modified_since({"modified_at": "2026-06-01T00:00:00Z"}, "modified_at", since) is False
    # missing / unparseable cursor value → keep the record (never silently drop)
    assert Component._is_modified_since({}, "modified_at", since) is True
    assert Component._is_modified_since({"modified_at": "not-a-date"}, "modified_at", since) is True
    # no incremental field configured → always keep
    assert Component._is_modified_since({"modified_at": "2026-06-01T00:00:00Z"}, None, since) is True


def test_since_returns_watermark_from_state():
    since = Component._since(True, {"last_run": "2026-07-24T00:00:00+00:00"}, "modified_at")
    assert since == datetime.fromisoformat("2026-07-24T00:00:00+00:00")


def test_since_none_on_first_run_full_load_or_reference():
    # first run, no prior watermark → full pull
    assert Component._since(True, {}, "modified_at") is None
    # full load → never filters
    assert Component._since(False, {"last_run": "2026-07-24T00:00:00+00:00"}, "modified_at") is None
    # reference object without a cursor → never filters
    assert Component._since(True, {"last_run": "2026-07-24T00:00:00+00:00"}, None) is None


def test_since_ignores_unreadable_state():
    assert Component._since(True, {"last_run": "not-a-date"}, "modified_at") is None


def test_incremental_filter_drops_stale_keeps_new():
    # End-to-end filter decision: given a prior watermark, records modified at or
    # before it are dropped and newer ones kept — the wiring _extract_object relies on.
    since = Component._since(True, {"last_run": "2026-07-24T00:00:00+00:00"}, "modified_at")
    assert since is not None
    stale = {"id": "1", "modified_at": "2026-07-21T14:30:35Z"}
    fresh = {"id": "2", "modified_at": "2026-07-25T00:00:00Z"}
    kept = [r for r in (stale, fresh) if Component._is_modified_since(r, "modified_at", since)]
    assert [r["id"] for r in kept] == ["2"]


# --------------------------------------------------------------- scoping


def test_resolve_path_user_scope():
    cfg = Configuration(**_CREDS, object="businesses")
    assert Component._resolve_path(cfg, _REGISTRY[ObjectType.businesses]) == "me/businesses"


def test_resolve_path_toplevel_scope():
    cfg = Configuration(**_CREDS, object="time_zones")
    assert Component._resolve_path(cfg, _REGISTRY[ObjectType.time_zones]) == "time_zones"


def test_resolve_path_business_scope_requires_business_id():
    ep = _REGISTRY[ObjectType.ad_accounts]
    ok = Configuration(**_CREDS, object="ad_accounts", business_id="b1")
    assert Component._resolve_path(ok, ep) == "businesses/b1/ad_accounts"
    with pytest.raises(UserException):
        Component._resolve_path(Configuration(**_CREDS, object="ad_accounts"), ep)


def test_resolve_path_account_scope_requires_ad_account_id():
    ep = _REGISTRY[ObjectType.campaigns]
    ok = Configuration(**_CREDS, object="campaigns", ad_account_id="a2_x")
    assert Component._resolve_path(ok, ep) == "ad_accounts/a2_x/campaigns"
    with pytest.raises(UserException):
        Component._resolve_path(Configuration(**_CREDS, object="campaigns"), ep)


def test_registry_covers_every_non_report_object():
    for obj in ObjectType:
        if obj != ObjectType.report:
            assert obj in _REGISTRY, f"{obj} missing from registry"


# --------------------------------------------------------------- output


def test_order_columns_pk_first_then_sorted():
    cols = {"zebra": None, "id": None, "alpha": None}
    assert Component._order_columns(cols, ["id"]) == ["id", "alpha", "zebra"]


def test_serialize_cell():
    assert Component._serialize_cell(None, False) == ""
    assert Component._serialize_cell(True, False) == "true"
    assert Component._serialize_cell(False, False) == "false"
    assert Component._serialize_cell("", True) == _EMPTY_PK_PLACEHOLDER
    assert Component._serialize_cell("keep", True) == "keep"


# --------------------------------------------------------------- reports


def test_report_primary_key_from_breakdowns_present_in_columns():
    rc = ReportConfig(metrics=["impressions"], breakdowns=["DATE", "CAMPAIGN_ID"])
    columns = {"date": None, "campaign_id": None, "impressions": None}
    assert Component._report_primary_key(rc, columns) == ["date", "campaign_id"]


def test_report_primary_key_empty_when_breakdowns_absent():
    rc = ReportConfig(metrics=["impressions"], breakdowns=["DATE"])
    assert Component._report_primary_key(rc, {"impressions": None}) == []


def test_report_body_shape():
    rc = ReportConfig(metrics=["impressions", "clicks"], breakdowns=["DATE"])
    body = Component._report_body(rc, date(2026, 7, 1), date(2026, 7, 8))
    assert body == {
        "fields": ["impressions", "clicks"],
        "breakdowns": ["DATE"],
        "starts_at": "2026-07-01T00:00:00Z",
        "ends_at": "2026-07-08T00:00:00Z",
    }


@freeze_time("2026-07-22")
def test_report_window_last_n_days():
    cfg = Configuration(
        **_CREDS,
        ad_account_id="a1",
        object="report",
        load_type="full_load",
        report={"metrics": ["impressions"], "n_days": 7},
    )
    start, end = _bare_component()._report_window(cfg, cfg.report, {})
    assert end == date(2026, 7, 22)
    assert start == date(2026, 7, 15)


@freeze_time("2026-07-22")
def test_report_window_incremental_applies_lookback():
    cfg = Configuration(
        **_CREDS,
        ad_account_id="a1",
        object="report",
        load_type="incremental_load",
        report={"metrics": ["impressions"], "n_days": 7, "lookback_window": 3},
    )
    state = {"last_run": "2026-07-20T00:00:00+00:00"}
    start, end = _bare_component()._report_window(cfg, cfg.report, state)
    assert start == date(2026, 7, 17)  # last_run - 3-day lookback
    assert end == date(2026, 7, 22)


def test_report_window_custom_range():
    cfg = Configuration(
        **_CREDS,
        ad_account_id="a1",
        object="report",
        report={
            "metrics": ["impressions"],
            "date_range_mode": "custom",
            "start_date": "2026-01-01",
            "end_date": "2026-01-31",
        },
    )
    start, end = _bare_component()._report_window(cfg, cfg.report, {})
    assert start == date(2026, 1, 1)
    assert end == date(2026, 1, 31)
