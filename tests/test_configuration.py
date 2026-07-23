import pytest
from keboola.component.exceptions import UserException

from configuration import Configuration, Credentials, LoadType, ObjectType, ReportConfig

_CREDS = {"#client_id": "cid", "#client_secret": "csec", "#refresh_token": "rt"}


def test_credentials_parse_with_aliases():
    creds = Credentials(**_CREDS)
    assert creds.client_id == "cid"
    assert creds.client_secret == "csec"
    assert creds.refresh_token == "rt"
    assert creds.user_agent  # a non-empty default


def test_credentials_missing_field_raises_userexception():
    with pytest.raises(UserException) as exc:
        Credentials(**{"#client_id": "cid", "#client_secret": "csec"})
    assert "refresh_token" in str(exc.value)


def test_credentials_ignore_extra_keys():
    # The platform injects extras (e.g. debug) that must not fail validation.
    creds = Credentials(**_CREDS, debug=True, ad_account_id="a1")
    assert creds.client_id == "cid"


def test_configuration_requires_account_and_object():
    with pytest.raises(UserException):
        Configuration(**_CREDS, object="campaigns")  # no ad_account_id
    with pytest.raises(UserException):
        Configuration(**_CREDS, ad_account_id="a1")  # no object


def test_configuration_report_requires_report_block():
    with pytest.raises(UserException):
        Configuration(**_CREDS, ad_account_id="a1", object="report")


def test_configuration_incremental_flag():
    cfg = Configuration(**_CREDS, ad_account_id="a1", object="campaigns", load_type="incremental_load")
    assert cfg.incremental is True
    cfg_full = Configuration(**_CREDS, ad_account_id="a1", object="campaigns", load_type="full_load")
    assert cfg_full.incremental is False


def test_reference_objects_forced_full_load():
    # Reference objects have no cursor → always full load, even if incremental is requested.
    cfg = Configuration(**_CREDS, ad_account_id="a1", object="ad_accounts", load_type="incremental_load")
    assert cfg.incremental is False


def test_report_config_valid():
    cfg = Configuration(
        **_CREDS,
        ad_account_id="a1",
        object="report",
        report={"level": "CAMPAIGN", "metrics": ["impressions", "clicks"], "breakdowns": ["date"]},
    )
    assert cfg.report is not None
    assert cfg.report.metrics == ["impressions", "clicks"]


def test_report_config_rejects_more_than_three_breakdowns():
    with pytest.raises(UserException):
        Configuration(
            **_CREDS,
            ad_account_id="a1",
            object="report",
            report={"metrics": ["impressions"], "breakdowns": ["a", "b", "c", "d"]},
        )


def test_report_config_requires_metrics():
    with pytest.raises(UserException):
        Configuration(**_CREDS, ad_account_id="a1", object="report", report={"metrics": [], "breakdowns": []})


def test_report_config_custom_range_requires_dates():
    with pytest.raises(UserException):
        Configuration(
            **_CREDS,
            ad_account_id="a1",
            object="report",
            report={"metrics": ["impressions"], "date_range_mode": "custom"},
        )


def test_enums_cover_expected_values():
    assert ObjectType.report == "report"
    assert LoadType.incremental_load == "incremental_load"
    assert ReportConfig(metrics=["impressions"]).level == "CAMPAIGN"
