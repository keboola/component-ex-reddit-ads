"""Configuration models for the Reddit Ads extractor.

All configuration is validated up-front via Pydantic; any validation error is
re-raised as a ``UserException`` so the platform surfaces it as a user error
(exit 1) instead of an unexpected crash (exit 2).

Layout mirrors how the platform runs the component: connection/auth lives at the
**config level** (``Credentials`` + ``ad_account_id``), while the object to extract
and its options live at the **row level** (config rows, one row per object).
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

    ``report`` is the performance/insights endpoint; every other member is an
    entity/reference collection. Values are the logical object names; the
    endpoint path and primary key for each live in the endpoint registry in
    ``component.py``.
    """

    ad_accounts = "ad_accounts"
    businesses = "businesses"
    members = "members"
    ad_account_history = "ad_account_history"
    campaigns = "campaigns"
    ad_groups = "ad_groups"
    ads = "ads"
    posts = "posts"
    report = "report"
    custom_audiences = "custom_audiences"
    saved_audiences = "saved_audiences"
    targeting = "targeting"
    pixels = "pixels"
    lead_gen_forms = "lead_gen_forms"
    product_catalogs = "product_catalogs"
    funding_instruments = "funding_instruments"
    app = "app"
    skadnetwork = "skadnetwork"
    profiles = "profiles"
    time_zones = "time_zones"


class LoadType(StrEnum):
    full_load = "full_load"
    incremental_load = "incremental_load"


class ReportLevel(StrEnum):
    ACCOUNT = "ACCOUNT"
    CAMPAIGN = "CAMPAIGN"
    AD_GROUP = "AD_GROUP"
    AD = "AD"


class Granularity(StrEnum):
    DAY = "DAY"
    HOUR = "HOUR"
    TOTAL = "TOTAL"


class DateRangeMode(StrEnum):
    last_n_days = "last_n_days"
    custom = "custom"


# Reddit Ads hard limit: at most 3 breakdowns per report request.
MAX_BREAKDOWNS = 3

# Reference objects with no useful incremental cursor — always full-loaded.
FULL_LOAD_ONLY_OBJECTS = frozenset(
    {
        ObjectType.ad_accounts,
        ObjectType.businesses,
        ObjectType.members,
        ObjectType.funding_instruments,
        ObjectType.targeting,
        ObjectType.time_zones,
    }
)


class ReportConfig(BaseModel):
    """Reporting parameters — only relevant when ``object == report``."""

    model_config = ConfigDict(extra="ignore")

    level: ReportLevel = ReportLevel.CAMPAIGN
    metrics: list[str] = Field(default_factory=list)
    breakdowns: list[str] = Field(default_factory=list)
    time_zone: str = "UTC"
    granularity: Granularity = Granularity.DAY
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

    The sync-action path (Test Connection / Load Ad Accounts) only needs
    credentials, so it validates just this model — an unrelated report validation
    error can never block a credential test. ``populate_by_name`` lets callers
    pass either the ``#``-aliased key or the plain field name.
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
    """Top-level (merged) component configuration for one config row."""

    ad_account_id: str = ""
    object: ObjectType | None = None
    load_type: LoadType = LoadType.incremental_load
    report: ReportConfig | None = None

    @model_validator(mode="after")
    def _validate_run(self) -> Configuration:
        if not self.ad_account_id:
            raise ValueError("An ad account must be selected (ad_account_id).")
        if self.object is None:
            raise ValueError("Select an object to extract.")
        if self.object == ObjectType.report and self.report is None:
            raise ValueError("Report configuration is required when the object is 'report'.")
        return self

    @computed_field
    @property
    def incremental(self) -> bool:
        # Reference objects have no cursor → always full load, regardless of the flag.
        if self.object in FULL_LOAD_ONLY_OBJECTS:
            return False
        return self.load_type == LoadType.incremental_load
