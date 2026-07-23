"""Configuration models for the Reddit Ads extractor.

All configuration is validated up-front via Pydantic; any validation error is
re-raised as a ``UserException`` so the platform surfaces it as a user error
(exit 1) instead of an unexpected crash (exit 2).

The Reddit Ads object hierarchy is user → business → ad account, so connection
lives at the **config level** (``Credentials`` + ``business_id`` + ``ad_account_id``)
and the object to extract lives at the **row level** (config rows, one per object).
Which id an object needs depends on its scope (see the endpoint registry in
``component.py``): business-scoped objects use ``business_id``, account-scoped
objects use ``ad_account_id``, user-scoped and reference objects use neither.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from keboola.component.exceptions import UserException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, computed_field, model_validator

# Default User-Agent sent to Reddit. Reddit rejects requests without a real,
# descriptive User-Agent, so this is not optional cosmetics — it must be present.
DEFAULT_USER_AGENT = "keboola-ex-reddit-ads/1.0"


class ObjectType(StrEnum):
    """The Reddit Ads read surface — one object per config row.

    Every value below is a verified live endpoint. ``report`` is the performance
    endpoint; the rest are entity/reference collections. The endpoint path, scope
    (user/business/account/top-level) and primary key for each live in the
    endpoint registry in ``component.py``.
    """

    # user-scoped
    businesses = "businesses"
    # business-scoped
    ad_accounts = "ad_accounts"
    product_catalogs = "product_catalogs"
    # account-scoped entities
    campaigns = "campaigns"
    ad_groups = "ad_groups"
    ads = "ads"
    custom_audiences = "custom_audiences"
    saved_audiences = "saved_audiences"
    funding_instruments = "funding_instruments"
    pixels = "pixels"
    lead_gen_forms = "lead_gen_forms"
    profiles = "profiles"
    apps = "apps"
    # account-scoped performance
    report = "report"
    # top-level reference
    targeting_interests = "targeting_interests"
    targeting_communities = "targeting_communities"
    targeting_geolocations = "targeting_geolocations"
    targeting_carriers = "targeting_carriers"
    targeting_devices = "targeting_devices"
    targeting_languages = "targeting_languages"
    time_zones = "time_zones"


class LoadType(StrEnum):
    full_load = "full_load"
    incremental_load = "incremental_load"


class DateRangeMode(StrEnum):
    last_n_days = "last_n_days"
    custom = "custom"


# Reddit Ads report hard limit: at most 3 breakdowns per request.
MAX_BREAKDOWNS = 3


class ReportConfig(BaseModel):
    """Reporting parameters — only relevant when ``object == report``.

    The report endpoint takes a set of metric ``fields`` and up to three
    ``breakdowns`` (grouping dimensions, e.g. DATE, CAMPAIGN_ID) over a date
    window. There is no separate "level" parameter — the grouping is expressed
    entirely through breakdowns.
    """

    model_config = ConfigDict(extra="ignore")

    metrics: list[str] = Field(default_factory=list)  # sent to the API as `fields`
    breakdowns: list[str] = Field(default_factory=lambda: ["DATE"])
    date_range_mode: DateRangeMode = DateRangeMode.last_n_days
    # Used when date_range_mode == last_n_days
    n_days: int = Field(default=7, ge=1, le=365)
    # Used when date_range_mode == custom (ISO date, YYYY-MM-DD, or a relative expression)
    start_date: str | None = None
    end_date: str | None = None
    # Extra trailing days re-pulled every run so late conversion attribution is upserted.
    lookback_window: int = Field(default=7, ge=0, le=90)

    @model_validator(mode="after")
    def _validate(self) -> ReportConfig:
        if not self.metrics:
            raise ValueError("At least one metric must be selected for a report.")
        if len(self.breakdowns) > MAX_BREAKDOWNS:
            raise ValueError(f"Reddit Ads allows at most {MAX_BREAKDOWNS} breakdowns per report.")
        if self.date_range_mode == DateRangeMode.custom and not (self.start_date and self.end_date):
            raise ValueError("start_date and end_date are required when date range mode is 'custom'.")
        return self


class Credentials(BaseModel):
    """OAuth 2.0 app credentials, parsed on their own.

    The sync-action path (Test Connection / Load Businesses / Load Ad Accounts)
    only needs credentials, so it validates just this model — an unrelated report
    validation error can never block a credential test. ``populate_by_name`` lets
    callers pass either the ``#``-aliased key or the plain field name.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    client_id: str = Field(alias="#client_id")
    client_secret: str = Field(alias="#client_secret")
    refresh_token: str = Field(alias="#refresh_token")
    user_agent: str = DEFAULT_USER_AGENT

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError as e:
            error_messages = [f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()]
            raise UserException(f"Configuration validation error: {', '.join(error_messages)}") from e


class Configuration(Credentials):
    """Top-level (merged) component configuration for one config row.

    ``business_id`` and ``ad_account_id`` are both optional here because which one
    an object needs depends on its scope; the component validates the right id is
    present for the chosen object (with a clear message) at dispatch time.
    """

    business_id: str = ""
    ad_account_id: str = ""
    object: ObjectType | None = None
    load_type: LoadType = LoadType.incremental_load
    report: ReportConfig | None = None

    @model_validator(mode="after")
    def _validate_run(self) -> Configuration:
        if self.object is None:
            raise ValueError("Select an object to extract.")
        if self.object == ObjectType.report and self.report is None:
            raise ValueError("Report configuration is required when the object is 'report'.")
        return self

    @computed_field
    @property
    def incremental(self) -> bool:
        return self.load_type == LoadType.incremental_load
