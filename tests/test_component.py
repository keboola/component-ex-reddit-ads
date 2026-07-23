from datetime import date

from freezegun import freeze_time

from component import _EMPTY_PK_PLACEHOLDER, Component
from configuration import Configuration, ReportConfig

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
    since = __import__("datetime").datetime.fromisoformat("2026-07-01T00:00:00+00:00")
    newer = {"modified_at": "2026-07-05T00:00:00Z"}
    older = {"modified_at": "2026-06-01T00:00:00Z"}
    assert Component._is_modified_since(newer, "modified_at", since) is True
    assert Component._is_modified_since(older, "modified_at", since) is False
    # missing / unparseable cursor value → keep the record (never silently drop)
    assert Component._is_modified_since({}, "modified_at", since) is True
    assert Component._is_modified_since({"modified_at": "not-a-date"}, "modified_at", since) is True
    # no incremental field configured → always keep
    assert Component._is_modified_since(older, None, since) is True


# --------------------------------------------------------------- output


def test_order_columns_pk_first_then_sorted():
    cols = {"zebra": None, "id": None, "alpha": None}
    assert Component._order_columns(cols, ["id"]) == ["id", "alpha", "zebra"]


def test_serialize_cell():
    assert Component._serialize_cell(None, False) == ""
    assert Component._serialize_cell(True, False) == "true"
    assert Component._serialize_cell(False, False) == "false"
    # empty PK cell gets the placeholder so the typed NOT NULL PK column loads
    assert Component._serialize_cell("", True) == _EMPTY_PK_PLACEHOLDER
    assert Component._serialize_cell("keep", True) == "keep"


# --------------------------------------------------------------- reports


def test_report_primary_key_uses_date_level_and_breakdowns():
    rc = ReportConfig(level="CAMPAIGN", metrics=["impressions"], breakdowns=["placement"])
    columns = {"date": None, "campaign_id": None, "placement": None, "impressions": None}
    pk = Component._report_primary_key(rc, columns)
    assert pk == ["date", "campaign_id", "placement"]


def test_report_primary_key_falls_back_to_date():
    rc = ReportConfig(level="AD", metrics=["impressions"], breakdowns=[])
    assert Component._report_primary_key(rc, {"impressions": None}) == ["date"]


def test_report_body_shape():
    rc = ReportConfig(level="CAMPAIGN", metrics=["impressions", "clicks"], breakdowns=["date"], time_zone="UTC")
    body = Component._report_body(rc, date(2026, 7, 1), date(2026, 7, 8))
    assert body["level"] == "CAMPAIGN"
    assert body["metrics"] == ["impressions", "clicks"]
    assert body["breakdowns"] == ["date"]
    assert body["starts_at"] == "2026-07-01"
    assert body["ends_at"] == "2026-07-08"


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
    # last_run (2026-07-20) minus 3-day lookback
    assert start == date(2026, 7, 17)
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
