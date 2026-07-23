"""Reddit Ads extractor — main component class.

Pulls the Reddit Ads read surface (accounts, the campaign→ad-group→ad→post
hierarchy, audiences, reference data and performance reports) into Keboola
Storage. ``run()`` is a thin orchestrator; all HTTP access lives in :mod:`client`.

One object is extracted per config row; the object → endpoint/PK/cursor mapping
lives in ``_REGISTRY``.
"""

from __future__ import annotations

import csv
import json
import logging
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import dateparser
from keboola.component.base import ComponentBase, sync_action
from keboola.component.dao import BaseType, ColumnDefinition
from keboola.component.exceptions import UserException
from keboola.component.sync_actions import MessageType, SelectElement, ValidationResult
from keboola.vcr import BodyFieldSanitizer, DefaultSanitizer

from client import RedditAdsClient
from configuration import Configuration, Credentials, DateRangeMode, ObjectType, ReportConfig

# VCR sanitizers, picked up by the datadirtest VCR scaffolder during recording.
# 1) DefaultSanitizer strips the Authorization header + the OAuth token fields.
# 2) BodyFieldSanitizer redacts account names/spend from recorded bodies.
#    NEVER redact id fields (`id`, `campaign_id`, …) or `date`/`modified_at` —
#    primary keys and the incremental window depend on them.
_REDACTED_TEXT_FIELDS = ["name", "business_name", "email", "advertiser_name"]
_REDACTED_NUMERIC_FIELDS = ["spend", "spend_micros", "budget", "daily_budget", "lifetime_budget"]
VCR_SANITIZERS = [
    DefaultSanitizer(additional_sensitive_fields=["access_token", "refresh_token", "client_id", "client_secret"]),
    BodyFieldSanitizer(fields=_REDACTED_TEXT_FIELDS, replacement="REDACTED"),
    BodyFieldSanitizer(fields=_REDACTED_NUMERIC_FIELDS, replacement="0"),
]

_STATE_LAST_RUN = "last_run"

# Placeholder for an otherwise-empty primary-key cell. Keboola typed-table PK
# columns are physically NOT NULL and an empty CSV field imports as NULL, so a
# blank PK value would fail the storage load.
_EMPTY_PK_PLACEHOLDER = "__empty__"

# Columns emitted with a known, stable native type. Everything else stays STRING
# (the Reddit response is wide and heterogeneous; over-typing risks load failures).
_TIMESTAMP_COLUMNS = frozenset({"created_at", "modified_at", "start_time", "end_time"})


@dataclass(frozen=True)
class _Endpoint:
    """How to fetch and store one object.

    ``path`` is relative to ``ad_accounts/{account_id}/`` when ``account_scoped``,
    otherwise it is a top-level path. ``incremental_field`` is the response field
    used as the client-side ``modified_at`` cursor (None → the object is always
    full-loaded).

    NOTE: exact endpoint paths / PKs for a few objects are provisional (spec §9)
    and localized here so a live-verified correction is a one-line change.
    """

    path: str
    primary_key: list[str] = field(default_factory=lambda: ["id"])
    account_scoped: bool = True
    incremental_field: str | None = "modified_at"


# object → endpoint mapping. Reference objects carry incremental_field=None.
_REGISTRY: dict[ObjectType, _Endpoint] = {
    ObjectType.ad_accounts: _Endpoint("ad_accounts", account_scoped=False, incremental_field=None),
    ObjectType.businesses: _Endpoint("businesses", account_scoped=False, incremental_field=None),
    ObjectType.members: _Endpoint("members", incremental_field=None),
    ObjectType.ad_account_history: _Endpoint("history", primary_key=["id"]),
    ObjectType.campaigns: _Endpoint("campaigns"),
    ObjectType.ad_groups: _Endpoint("ad_groups"),
    ObjectType.ads: _Endpoint("ads"),
    ObjectType.posts: _Endpoint("posts"),
    ObjectType.custom_audiences: _Endpoint("custom_audiences"),
    ObjectType.saved_audiences: _Endpoint("saved_audiences"),
    ObjectType.targeting: _Endpoint("targeting", incremental_field=None),
    ObjectType.pixels: _Endpoint("pixels"),
    ObjectType.lead_gen_forms: _Endpoint("lead_gen_forms"),
    ObjectType.product_catalogs: _Endpoint("product_catalogs"),
    ObjectType.funding_instruments: _Endpoint("funding_instruments", incremental_field=None),
    ObjectType.app: _Endpoint("apps"),
    ObjectType.skadnetwork: _Endpoint("skadnetwork"),
    ObjectType.profiles: _Endpoint("profiles"),
    ObjectType.time_zones: _Endpoint("time_zones", account_scoped=False, incremental_field=None),
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
        # run() and the sync actions. Validating just the credentials keeps Test
        # Connection / Load Ad Accounts from tripping over an unrelated report
        # validation error. run() parses the full Configuration.
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
        since = self._since(cfg, previous_state, endpoint.incremental_field)

        writer = _TableWriter(obj.value)
        try:
            path = f"ad_accounts/{cfg.ad_account_id}/{endpoint.path}" if endpoint.account_scoped else endpoint.path
            for record in self._client.get_paginated(path):
                if since and not self._is_modified_since(record, endpoint.incremental_field, since):
                    continue
                writer.write(self._flatten_record(record))
            self._finalize_table(writer, primary_key=endpoint.primary_key, incremental=cfg.incremental)
        finally:
            writer.close()

    def _since(
        self, cfg: Configuration, previous_state: dict[str, Any], incremental_field: str | None
    ) -> datetime | None:
        """The lower-bound timestamp for an incremental entity pull, or None for full load."""
        if not cfg.incremental or incremental_field is None:
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
        start, end = self._report_window(cfg, rc, previous_state)
        body = self._report_body(rc, start, end)

        writer = _TableWriter(f"report_{rc.level.value.lower()}")
        try:
            for row in self._client.get_report(cfg.ad_account_id, body):
                writer.write(self._flatten_record(row))
            # PK is provisional (spec §9): a report row is uniquely keyed by its
            # date, the entity id at the report level, and any breakdown columns.
            pk = self._report_primary_key(rc, writer.columns)
            self._finalize_table(writer, primary_key=pk, incremental=cfg.incremental, typed=False)
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
        # Payload param names are provisional (spec §9) — isolated here for an easy fix.
        return {
            "level": rc.level.value,
            "metrics": rc.metrics,
            "breakdowns": rc.breakdowns,
            "time_zone_id": rc.time_zone,
            "time_granularity": rc.granularity.value,
            "starts_at": start.isoformat(),
            "ends_at": end.isoformat(),
        }

    @staticmethod
    def _report_primary_key(rc: ReportConfig, columns: dict[str, None]) -> list[str]:
        pk: list[str] = [c for c in ("date", f"{rc.level.value.lower()}_id", "id") if c in columns]
        pk += [b for b in rc.breakdowns if b in columns and b not in pk]
        return pk or ["date"]

    # ---------------------------------------------------------- flattening

    @staticmethod
    def _flatten_record(record: dict[str, Any]) -> dict[str, Any]:
        """Flatten one record; nested objects/lists become JSON strings."""
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
            self._client.list_ad_accounts()
        except UserException:
            raise
        except Exception as exc:
            raise UserException(f"Connection test failed: {exc}") from exc
        return ValidationResult("Connection successful.", MessageType.SUCCESS)

    @sync_action("listAdAccounts")
    def list_ad_accounts_action(self) -> list[SelectElement]:
        try:
            accounts = self._client.list_ad_accounts()
        except UserException:
            raise
        except Exception as exc:
            raise UserException(f"Could not load ad accounts: {exc}") from exc
        return [
            SelectElement(value=acc["id"], label=f"{acc.get('name', acc['id'])} ({acc['id']})")
            for acc in accounts
            if acc.get("id")
        ]

    @sync_action("listTimeZones")
    def list_time_zones_action(self) -> list[SelectElement]:
        try:
            zones = self._client.list_time_zones()
        except UserException:
            raise
        except Exception as exc:
            raise UserException(f"Could not load time zones: {exc}") from exc
        options: list[SelectElement] = []
        for z in zones:
            value = z.get("id") or z.get("name")
            if not value:
                continue
            options.append(SelectElement(value=value, label=z.get("name") or value))
        return options


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
