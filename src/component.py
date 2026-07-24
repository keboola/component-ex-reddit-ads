"""Reddit Ads extractor — main component class.

Pulls the Reddit Ads read surface into Keboola Storage. ``run()`` is a thin
orchestrator; all HTTP access lives in :mod:`client`.

The Reddit Ads hierarchy is user → business → ad account, so objects have a
**scope**: user-scoped (``me/businesses``), business-scoped
(``businesses/{business_id}/…``), account-scoped (``ad_accounts/{ad_account_id}/…``)
or top-level reference (``time_zones``, ``targeting/…``). One object is extracted
per config row; the object → path/scope/PK/cursor mapping lives in ``_REGISTRY``.
"""

from __future__ import annotations

import csv
import json
import logging
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Any

import dateparser
from keboola.component.base import ComponentBase, sync_action
from keboola.component.dao import BaseType, ColumnDefinition
from keboola.component.exceptions import UserException
from keboola.component.sync_actions import MessageType, SelectElement, ValidationResult
from keboola.vcr import DefaultSanitizer

from client import RedditAdsClient
from configuration import (
    BusinessSelection,
    Configuration,
    Credentials,
    DateRangeMode,
    ObjectType,
    ReportConfig,
)

# VCR sanitizers, picked up by the datadirtest VCR scaffolder during recording.
# DefaultSanitizer strips the Authorization header (the Basic client_id:client_secret
# on the token exchange + the bearer on API calls) and redacts the OAuth token fields
# in the token-exchange request/response body — the only true secrets.
#
# We deliberately do NOT redact response *content* fields (names, phone, spend, …):
# the scaffolder builds each test's `expected/` output from the live recording run
# while the cassette is sanitized, so redacting any field that is also an output column
# makes replay output diverge from `expected/` (a guaranteed test failure). The recorded
# data here is Keboola's own low-spend test account plus public reference data
# (time zones, targeting), so no sensitive content is committed.
# Keep every real-secret field DefaultSanitizer redacts, but DROP "code": the only
# `code` in this component's traffic is the time-zone `code` (GMT/EET…), a plain output
# column — the OAuth authorization code never appears at runtime (we use refresh tokens).
# Redacting it would corrupt the time_zones output and break replay.
_SENSITIVE_FIELDS = [
    "access_token",
    "refresh_token",
    "id_token",
    "client_id",
    "client_secret",
    "client_assertion",
    "password",
    "token",
    # `email` appears only in the /me response (test-connection); it is never an output
    # column, so redacting it scrubs the account email from committed cassettes safely.
    "email",
]
VCR_SANITIZERS = [
    DefaultSanitizer(sensitive_fields=_SENSITIVE_FIELDS),
]

_STATE_LAST_RUN = "last_run"

# Placeholder for an otherwise-empty primary-key cell. Keboola typed-table PK
# columns are physically NOT NULL and an empty CSV field imports as NULL, so a
# blank PK value would fail the storage load.
_EMPTY_PK_PLACEHOLDER = "__empty__"

# Columns emitted with a known, stable native type. Everything else stays STRING
# (the Reddit response is wide and heterogeneous; over-typing risks load failures).
_TIMESTAMP_COLUMNS = frozenset({"created_at", "modified_at", "start_time", "end_time"})


class Scope(Enum):
    USER = auto()  # me/<path> (e.g. me/businesses)
    BUSINESS = auto()  # businesses/{business_id}/<path>
    ACCOUNT = auto()  # ad_accounts/{ad_account_id}/<path>
    TOPLEVEL = auto()  # <path> as-is (reference data)


@dataclass(frozen=True)
class _Endpoint:
    """How to fetch and store one object. ``incremental_field`` = the response
    field used as the client-side cursor (None → always full-loaded)."""

    path: str
    scope: Scope
    primary_key: list[str] = field(default_factory=lambda: ["id"])
    incremental_field: str | None = "modified_at"


# object → endpoint mapping. All paths verified live against the Reddit Ads API v3.
_REGISTRY: dict[ObjectType, _Endpoint] = {
    ObjectType.businesses: _Endpoint("me/businesses", Scope.USER),
    ObjectType.ad_accounts: _Endpoint("ad_accounts", Scope.BUSINESS),
    ObjectType.product_catalogs: _Endpoint("product_catalogs", Scope.BUSINESS),
    ObjectType.campaigns: _Endpoint("campaigns", Scope.ACCOUNT),
    ObjectType.ad_groups: _Endpoint("ad_groups", Scope.ACCOUNT),
    ObjectType.ads: _Endpoint("ads", Scope.ACCOUNT),
    ObjectType.custom_audiences: _Endpoint("custom_audiences", Scope.ACCOUNT),
    ObjectType.saved_audiences: _Endpoint("saved_audiences", Scope.ACCOUNT),
    ObjectType.funding_instruments: _Endpoint("funding_instruments", Scope.ACCOUNT, incremental_field=None),
    ObjectType.pixels: _Endpoint("pixels", Scope.ACCOUNT, incremental_field=None),
    ObjectType.lead_gen_forms: _Endpoint("lead_gen_forms", Scope.ACCOUNT),
    ObjectType.profiles: _Endpoint("profiles", Scope.ACCOUNT, incremental_field=None),
    ObjectType.apps: _Endpoint("apps", Scope.ACCOUNT, incremental_field=None),
    ObjectType.targeting_interests: _Endpoint("targeting/interests", Scope.TOPLEVEL, incremental_field=None),
    ObjectType.targeting_communities: _Endpoint("targeting/communities", Scope.TOPLEVEL, incremental_field=None),
    ObjectType.targeting_geolocations: _Endpoint("targeting/geolocations", Scope.TOPLEVEL, incremental_field=None),
    ObjectType.targeting_carriers: _Endpoint("targeting/carriers", Scope.TOPLEVEL, incremental_field=None),
    ObjectType.targeting_devices: _Endpoint(
        "targeting/devices", Scope.TOPLEVEL, primary_key=["make", "model"], incremental_field=None
    ),
    ObjectType.targeting_languages: _Endpoint(
        "targeting/languages", Scope.TOPLEVEL, primary_key=["code"], incremental_field=None
    ),
    ObjectType.time_zones: _Endpoint("time_zones", Scope.TOPLEVEL, incremental_field=None),
}


class _TableWriter:
    """Spools rows to a temp NDJSON file while tracking the column superset.

    Keeps memory bounded on large pulls: rows are appended to /tmp as fetched,
    then streamed back into the output CSV by ``Component._finalize_table``.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.columns: dict[str, None] = {}  # insertion-ordered set of every key seen
        self.count = 0
        self._fh = tempfile.NamedTemporaryFile(
            mode="w",
            prefix=f"{name}_",
            suffix=".ndjson",
            dir=tempfile.gettempdir(),
            delete=False,
            encoding="utf-8",
            newline="",
        )
        self.path = Path(self._fh.name)

    def write(self, row: dict[str, Any]) -> None:
        for key in row:
            self.columns.setdefault(key, None)
        self._fh.write(json.dumps(row))
        self._fh.write("\n")
        self.count += 1

    def rows(self) -> Iterator[dict[str, Any]]:
        self._fh.flush()
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    yield json.loads(line)

    def close(self) -> None:
        self._fh.close()
        self.path.unlink(missing_ok=True)


class Component(ComponentBase):
    def __init__(self) -> None:
        super().__init__()
        # Parse only the credentials here and build the client once, shared by
        # run() and the sync actions. Validating just the credentials keeps the
        # sync actions from tripping over an unrelated report validation error.
        creds = Credentials(**self.configuration.parameters)
        self._client = RedditAdsClient(
            creds.client_id,
            creds.client_secret,
            creds.refresh_token,
            user_agent=creds.user_agent,
        )

    # ----------------------------------------------------------------- run

    def run(self) -> None:
        cfg = Configuration(**self.configuration.parameters)

        # Capture the watermark BEFORE fetching so anything changing mid-run is re-fetched next time.
        run_started_at = datetime.now(UTC)
        previous_state = self.get_state_file() or {}

        assert cfg.object is not None  # guaranteed by Configuration validation
        if cfg.object == ObjectType.report:
            self._extract_report(cfg, previous_state)
        else:
            self._extract_object(cfg, previous_state)

        # Advance the watermark only after successful writes.
        self.write_state_file({_STATE_LAST_RUN: run_started_at.isoformat()})
        logging.info("Extraction finished.")

    # ------------------------------------------------------------ entities

    def _extract_object(self, cfg: Configuration, previous_state: dict[str, Any]) -> None:
        obj = cfg.object
        assert obj is not None
        endpoint = _REGISTRY[obj]
        path = self._resolve_path(cfg, endpoint)
        since = self._since(cfg.incremental, previous_state, endpoint.incremental_field)

        writer = _TableWriter(obj.value)
        try:
            for record in self._client.get_paginated(path):
                if since and not self._is_modified_since(record, endpoint.incremental_field, since):
                    continue
                writer.write(self._flatten_record(record))
            incremental = cfg.incremental and endpoint.incremental_field is not None
            self._finalize_table(writer, primary_key=endpoint.primary_key, incremental=incremental)
        finally:
            writer.close()

    @staticmethod
    def _resolve_path(cfg: Configuration, endpoint: _Endpoint) -> str:
        """Build the request path for an object, enforcing the required id per scope."""
        if endpoint.scope == Scope.BUSINESS:
            if not cfg.business_id:
                raise UserException(f"A business must be selected to extract '{cfg.object}'.")
            return f"businesses/{cfg.business_id}/{endpoint.path}"
        if endpoint.scope == Scope.ACCOUNT:
            if not cfg.ad_account_id:
                raise UserException(f"An ad account must be selected to extract '{cfg.object}'.")
            return f"ad_accounts/{cfg.ad_account_id}/{endpoint.path}"
        return endpoint.path  # USER / TOPLEVEL paths are absolute

    @staticmethod
    def _since(incremental: bool, previous_state: dict[str, Any], incremental_field: str | None) -> datetime | None:
        """The lower-bound timestamp for an incremental entity pull, or None for full load."""
        if not incremental or incremental_field is None:
            return None
        last = previous_state.get(_STATE_LAST_RUN)
        if not last:
            return None
        try:
            return datetime.fromisoformat(last)
        except TypeError, ValueError:
            logging.warning("Ignoring unreadable %s state value %r; doing a full pull.", _STATE_LAST_RUN, last)
            return None

    @staticmethod
    def _is_modified_since(record: dict[str, Any], incremental_field: str | None, since: datetime) -> bool:
        if incremental_field is None:
            return True
        raw = record.get(incremental_field)
        if not raw:
            return True  # no cursor value → keep it, rather than silently drop
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")) > since
        except ValueError:
            return True

    # ------------------------------------------------------------- reports

    def _extract_report(self, cfg: Configuration, previous_state: dict[str, Any]) -> None:
        rc = cfg.report
        assert rc is not None  # guaranteed by Configuration validation
        if not cfg.ad_account_id:
            raise UserException("An ad account must be selected to extract a report.")
        start, end = self._report_window(cfg, rc, previous_state)
        report_data = self._report_body(rc, start, end)

        writer = _TableWriter("report")
        try:
            for row in self._client.get_report(cfg.ad_account_id, report_data):
                writer.write(self._flatten_record(row))
            pk = self._report_primary_key(rc, writer.columns)
            # Without a resolvable PK we overwrite (full) rather than append unbounded.
            self._finalize_table(writer, primary_key=pk, incremental=cfg.incremental and bool(pk), typed=False)
        finally:
            writer.close()

    def _report_window(self, cfg: Configuration, rc: ReportConfig, previous_state: dict[str, Any]) -> tuple[date, date]:
        if rc.date_range_mode == DateRangeMode.custom:
            start, end = self._parse_day(rc.start_date), self._parse_day(rc.end_date)
            if start > end:
                raise UserException(f"Report start date ({start}) must not be after end date ({end}).")
            return start, end

        end = datetime.now(UTC).date()
        start = end - timedelta(days=rc.n_days)
        if cfg.incremental:
            last = previous_state.get(_STATE_LAST_RUN)
            if last:
                try:
                    last_day = datetime.fromisoformat(last).date() - timedelta(days=rc.lookback_window)
                    start = min(last_day, end)
                except TypeError, ValueError:
                    logging.warning("Unreadable %s state; using the default report window.", _STATE_LAST_RUN)
        return start, end

    @staticmethod
    def _report_body(rc: ReportConfig, start: date, end: date) -> dict[str, Any]:
        # Verified live shape: metrics are `fields`, dates are date-times, `breakdowns`
        # are the grouping dimensions. The caller wraps this in {"data": ...}.
        return {
            "fields": rc.metrics,
            "breakdowns": rc.breakdowns,
            "starts_at": f"{start.isoformat()}T00:00:00Z",
            "ends_at": f"{end.isoformat()}T00:00:00Z",
        }

    @staticmethod
    def _report_primary_key(rc: ReportConfig, columns: dict[str, None]) -> list[str]:
        # A report row is keyed by its breakdown columns (e.g. DATE → `date`,
        # CAMPAIGN_ID → `campaign_id`). Use only breakdowns that actually appear as
        # columns; if none do, return [] so the table is overwritten, not appended.
        return [b.lower() for b in rc.breakdowns if b.lower() in columns]

    # ---------------------------------------------------------- flattening

    @staticmethod
    def _flatten_record(record: dict[str, Any]) -> dict[str, Any]:
        """Flatten one record; nested objects and lists become JSON-string columns.

        Deliberate decision: nested structures are serialized to a single JSON column
        rather than exploded into dotted scalar columns. Reddit Ads nested shapes
        vary across objects, so dotted flattening would yield an unstable, sparse
        schema. A single JSON column keeps the table schema stable and lets downstream
        transformations parse what they need. Matches the sibling `new-x-ads` extractor.
        """
        return {k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in record.items()}

    # -------------------------------------------------------------- output

    def _finalize_table(
        self, writer: _TableWriter, primary_key: list[str], incremental: bool, typed: bool = True
    ) -> None:
        if writer.count == 0:
            logging.info("No rows for %s; skipping table.", writer.name)
            return
        columns = self._order_columns(writer.columns, primary_key)
        base_type = self._base_type_for if typed else (lambda _col: BaseType.string())
        schema = {col: ColumnDefinition(data_types=base_type(col)) for col in columns}
        table = self.create_out_table_definition(
            f"{writer.name}.csv", primary_key=primary_key, incremental=incremental, schema=schema
        )
        pk_columns = set(primary_key)
        # Headerless CSV: the `schema` names the columns, so we must NOT write a header
        # row (has_header defaults to False under an authoritative manifest).
        with open(table.full_path, "w", encoding="utf-8", newline="") as fh:
            csv_writer = csv.writer(fh)
            for row in writer.rows():
                csv_writer.writerow([self._serialize_cell(row.get(col, ""), col in pk_columns) for col in columns])
        self.write_manifest(table)
        logging.info("Wrote %d rows to %s.", writer.count, writer.name)

    @staticmethod
    def _base_type_for(name: str) -> BaseType:
        if name in _TIMESTAMP_COLUMNS:
            return BaseType.timestamp()
        return BaseType.string()

    @staticmethod
    def _order_columns(columns: Iterable[str], primary_key: list[str]) -> list[str]:
        # Deterministic order: primary key first, then remaining fields alphabetically.
        seen = set(primary_key)
        extra = sorted(col for col in columns if col not in seen)
        return list(primary_key) + extra

    @staticmethod
    def _serialize_cell(value: object, is_primary_key: bool) -> object:
        if value is None:
            value = ""
        elif isinstance(value, bool):
            value = "true" if value else "false"
        if is_primary_key and (value is None or value == ""):
            return _EMPTY_PK_PLACEHOLDER
        return value

    # --------------------------------------------------------------- utils

    @staticmethod
    def _parse_day(value: str | None) -> date:
        if not value:
            raise UserException("A date is required but was empty.")
        parsed = dateparser.parse(str(value), settings={"RETURN_AS_TIMEZONE_AWARE": False})
        if parsed is None:
            raise UserException(
                f"Could not parse date '{value}'. Use YYYY-MM-DD or a relative date such as "
                f"'yesterday' or '30 days ago'."
            )
        return parsed.date()

    # ------------------------------------------------------- sync actions

    @sync_action("testConnection")
    def test_connection(self) -> ValidationResult:
        try:
            self._client.get_me()
        except UserException:
            raise
        except Exception as exc:
            raise UserException(f"Connection test failed: {exc}") from exc
        return ValidationResult("Connection successful.", MessageType.SUCCESS)

    @sync_action("listBusinesses")
    def list_businesses_action(self) -> list[SelectElement]:
        try:
            businesses = list(self._client.get_paginated(_REGISTRY[ObjectType.businesses].path))
        except UserException:
            raise
        except Exception as exc:
            raise UserException(f"Could not load businesses: {exc}") from exc
        return [
            SelectElement(value=b["id"], label=f"{b.get('name', b['id'])} ({b['id']})")
            for b in businesses
            if b.get("id")
        ]

    @sync_action("listAdAccounts")
    def list_ad_accounts_action(self) -> list[SelectElement]:
        business_id = BusinessSelection(**self.configuration.parameters).business_id
        if not business_id:
            raise UserException("Select a business first, then load its ad accounts.")
        try:
            account_path = f"businesses/{business_id}/{_REGISTRY[ObjectType.ad_accounts].path}"
            accounts = list(self._client.get_paginated(account_path))
        except UserException:
            raise
        except Exception as exc:
            raise UserException(f"Could not load ad accounts: {exc}") from exc
        return [
            SelectElement(value=a["id"], label=f"{a.get('name', a['id'])} ({a['id']})") for a in accounts if a.get("id")
        ]


if __name__ == "__main__":
    try:
        comp = Component()
        comp.execute_action()
    except UserException as exc:
        logging.error(str(exc))
        exit(1)
    except Exception as exc:
        logging.exception(exc)
        exit(2)
