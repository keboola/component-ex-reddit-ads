# ex-reddit-ads — Design Spec

> Type: extractor
> Component ID: keboola.ex-reddit-ads
> Status: draft
> Date: 2026-07-22

## 1. Overview & source system

A Keboola extractor that pulls advertising data out of the **Reddit Ads API v3** — the full read
surface: account/business structure, the campaign→ad-group→ad→post hierarchy, audiences, targeting,
pixels, lead-gen, catalogs, billing, and the reporting/insights endpoint (metrics by
account/campaign/ad-group/ad level). It authenticates via OAuth 2.0 (refresh-token, headless after a
one-time grant) and writes one Storage table per object, with incremental loads where the API
supports a cursor.

- **Source system:** Reddit Ads API v3 — REST/JSON, base `https://ads-api.reddit.com/api/v3`.
  Docs: `https://ads-api.reddit.com/docs/v3/` · Official Postman workspace:
  `https://www.postman.com/reddit-ads-api/reddit-ads-api-v3`.
- **Primary use case:** land Reddit advertising performance and structure data in Keboola Storage for
  cross-channel marketing analytics (blending with other ad platforms, spend/ROAS reporting).

## 2. Keboola mapping

How Reddit Ads maps onto how Keboola runs a component.

- **Objects → output tables.** Each capability (see §4) becomes one output table, e.g.
  `campaigns.csv`, `ad_groups.csv`, `ads.csv`, `report_campaign.csv`. Report tables are named by
  level (`report_account` / `report_campaign` / `report_ad_group` / `report_ad`).
- **Config rows, one row per object** *(Tier-A convention: multiple independent objects → config
  rows, not a multipick)*. One row = "extract object X" (or "run report at level Y"). Each row gets
  independent enable/schedule/retry and its **own `state.json`** (per-row incremental watermark).
  Report rows carry the reporting parameters; entity rows are minimal.
- **Config-level vs row-level.** Auth (`#client_id`, `#client_secret`, `#refresh_token`) and the
  target `ad_account_id` live at **config level** (entered once). Object selection and per-object
  options (report level/metrics/breakdowns/date-range, `load_type`) live at **row level**.
- **Incremental strategy → output mapping + `state.json`.**
  - *Entities with a `modified_at` cursor* (campaigns, ad_groups, ads, posts, …): incremental load,
    PK `id`, watermark `last_run` (UTC ISO-8601, captured before fetch, persisted after success).
  - *Ad Account History* (append-only change log): incremental by event timestamp.
  - *Small reference objects* (ad_accounts, businesses, members, funding_instruments, targeting,
    time_zones): **full load** each run (tiny, no useful cursor) — reasoning stated per row.
  - *Reports*: re-pull by **date window** with a configurable lookback (late conversion attribution);
    watermark = last synced date; PK = composite (date + entity id + breakdown keys); incremental
    upsert.
- **Secrets → `#`-prefixed** so the platform encrypts them (`KBC::ProjectSecure`). Runtime receives
  plaintext.
- **Sync actions:** `test-connection` (validate OAuth + reach the account); `list-ad-accounts`
  dropdown for `ad_account_id`; `list-time-zones` dropdown for the report timezone (backed by the
  Time Zones endpoint). Report `level` and `load_type` are static enum dropdowns.
- **Output bucket / table naming:** rely on **default bucket** (`in.c-keboola.ex-reddit-ads-<config>`)
  — set `defaultBucket=true` / `defaultBucketStage="in"` in the Dev Portal (Phase 6), write **bare
  table names**, don't hardcode a destination.
- **Native (authoritative) data types → requires a Dev Portal switch.** Emitting a `schema` manifest
  is not enough on its own: the platform only sends `KBC_DATA_TYPE_SUPPORT=authoritative` when the
  component's **`dataTypeSupport`** property is set to `authoritative` in the Dev Portal (new
  components default to `none`, which silently downgrades a correct `schema` manifest to legacy
  `column_metadata` hints). Phase 6 must
  `kbagent dev-portal patch --app keboola.ex-reddit-ads --property dataTypeSupport --value authoritative`
  (dry-run → TTY confirm → verify with a fresh GET), and code + switch must agree.

## 3. Authentication & connection

- **Auth method:** OAuth 2.0 **authorization-code grant with refresh token**. This is the *only*
  grant Reddit documents for the Ads API (no client-credentials / M2M). After a **one-time** human
  browser consent (`duration=permanent`, scope `adsread`), the component runs **fully headless**:
  each run exchanges the stored `refresh_token` for a fresh `access_token` (Basic-auth to
  `https://www.reddit.com/api/v1/access_token`) and calls the API with `Authorization: bearer …`.
  Access tokens last 1h/1d; refresh indefinitely.
- **Connection method:** REST over HTTPS, JSON. Thin custom `requests`-based client (no official
  Python SDK; PRAW is the content API, not ads).
- **Scopes:** `adsread` for structure + reporting. (`adsmeasurement:read` only if conversion/CAPI
  read data is later added — out of scope here.)
- **Provisioning (manual, one-time, customer-side — cannot be automated):**
  1. Reddit Ads account with a **Business Admin**.
  2. Business → Developer Portal → create a Developer App (name, About URL, redirect URI
     `https://oauth.pstmn.io/v1/callback`, primary contact) → `client_id` + `client_secret`.
  3. Browser-authorize once (`https://www.reddit.com/api/v1/authorize?duration=permanent`,
     `scope=adsread`, Basic-auth token exchange) → `refresh_token` (Postman recommended).
  4. Provide `client_id`, `client_secret`, `refresh_token`, `ad_account_id`.
- **Blockers / access:** **No sandbox exists** — recording VCR cassettes and the cf-dev smoke test
  require a real Reddit Ads account. The user is creating a Developer App and will authorize via
  Postman, then provide credentials for local development. Until then, cassettes are hand-seeded from
  documented response shapes (see §7 / §9).

## 4. Capability inventory & scope

Full read surface (user chose **Full read surface**, 2026-07-22). Every capability from the Phase 2
research inventory appears below with a verdict. The four Excluded rows are write/interactive
endpoints that produce no dataset — the user signed off on excluding them (2026-07-22).

| # | Capability (data family / endpoint) | Verdict | Rationale |
|---|---|---|---|
| 1 | Ad Accounts | In scope | account structure (full load) |
| 2 | Businesses | In scope | org owning accounts (full load) |
| 3 | Members | In scope | team/permissions (full load) |
| 4 | Ad Account History | In scope | change-log; incremental by event timestamp |
| 5 | Campaigns | In scope | incremental by `modified_at`, PK `id` |
| 6 | Ad Groups | In scope | incremental by `modified_at`, PK `id` |
| 7 | Ads | In scope | incremental by `modified_at`, PK `id` |
| 8 | Posts (creatives) | In scope | creative content behind ads |
| 9 | Reports | In scope | **core**; levels account/campaign/ad-group/ad, metrics, ≤3 breakdowns |
| 10 | Custom Audiences | In scope | audience lists |
| 11 | Saved Audiences | In scope | reusable segments |
| 12 | Targeting | In scope | reference lookups (geo/interests/communities/devices/languages) |
| 13 | Pixels | In scope | Reddit Pixel definitions |
| 14 | Lead Generation Forms | In scope | forms (+ submitted leads if the endpoint exposes them) |
| 15 | Product Catalogs | In scope | product feeds for dynamic ads |
| 16 | Funding Instruments | In scope | billing/payment methods (full load) |
| 17 | App | In scope | registered mobile apps |
| 18 | SKAdNetwork | In scope | iOS attribution config |
| 19 | Profiles | In scope | user/community lookups (where retrievable for the account) |
| 20 | Time Zones | In scope | reference lookup; also backs the report-timezone dropdown |
| 21 | Bid Suggestions | Excluded | interactive lookup requiring inputs — not a dataset (user signed off 2026-07-22) |
| 22 | Forecasting | Excluded | planning/what-if tool requiring inputs — not a dataset (user signed off 2026-07-22) |
| 23 | Creative Generator | Excluded | AI creative *generation* helper (write) — not extractable data (user signed off 2026-07-22) |
| 24 | Conversions (CAPI) | Excluded | server-side event *posting* (write-side) — not an extractor concern (user signed off 2026-07-22) |

Mechanics for the in-scope surface:
- **Pagination:** opaque cursor — response carries a `pagination` object with `next_url`/`previous_url`;
  the client follows `next_url` **verbatim** until absent. Never reconstruct cursor query params.
- **Rate limits:** per-endpoint-group pooled quota, per authorized app instance; limits returned in
  response headers. On `429`, exponential backoff + retry; surface persistent throttling as a
  `UserException`. (Exact numeric limits are an open item — §9.)
- **Response shapes / nesting:** list envelope `{"data":[...],"pagination":{...}}`. Nested objects
  (e.g. targeting spec inside an ad group) → JSON-stringify the nested field into a column, or flatten
  well-known scalars; decide per object at implementation from real payloads. Report rows: one row per
  (date × entity × breakdown combo).
- **Hashed custom-conversion names** in reports resolve to numbers → optional mapping pass (documented
  as a known wrinkle; not blocking).

## 5. Configuration & schema

**Config-level (root) parameters:**
- `#client_id`, `#client_secret`, `#refresh_token` — OAuth secrets (required).
- `ad_account_id` — target account (required; `list-ad-accounts` dropdown).

**Row-level parameters:**
- `object` — which capability to extract (enum dropdown of the 20 in-scope objects).
- `load_type` — `full_load` | `incremental_load` (default `incremental_load`; forced to `full_load`
  for reference objects that lack a cursor).
- **Report-only fields** (shown via `options.dependencies` when `object == "report"`):
  - `level` — `ACCOUNT` | `CAMPAIGN` | `AD_GROUP` | `AD`.
  - `metrics` — multi-select of supported metrics (static enum until the exact list is confirmed).
  - `breakdowns` — up to **3** (API hard limit).
  - `time_zone` — dropdown from `list-time-zones`.
  - `date_range` — relative (`last_7_days`, `last_30_days`, …) or explicit `date_from`/`date_to`.
  - `lookback_window` — days to re-pull for late attribution (default e.g. 7).
  - `time_granularity` — `DAY` (default) | others if supported.

**Sync actions:** `test-connection`, `list-ad-accounts`, `list-time-zones`.

**Secrets:** all three OAuth fields are `#`-prefixed.

**Handoff:** the actual `configSchema.json` / `configRowSchema.json` (including the
`options.dependencies` conditional report fields and the sync-action wiring) is built by
`component-build-ui` in Phase 6 — this section describes the fields, not the JSON.

## 6. Code architecture

- **`src/client/reddit_ads_client.py`** — API client, separate from `component.py`:
  - OAuth token refresh (Basic-auth token exchange, cache access token for the run).
  - `_request()` with retry/backoff on `429`/`5xx`, honoring rate-limit headers; raises typed errors.
  - Cursor pagination generator that follows `next_url` verbatim and yields records from `data`.
  - Per-family methods (`list_campaigns`, `list_ad_groups`, …, `get_report`).
- **`src/configuration.py`** — Pydantic models: `Configuration` (root: secrets + `ad_account_id`),
  `RowConfiguration` (object, `load_type` with a `computed_field incremental`, report sub-model).
  Validated early; validation errors → `UserException`.
- **`src/component.py`** — `run()` is a **thin orchestrator**: parse config → build client → look up
  the object in an **endpoint registry** (object → path, PK, incremental field, table name) → fetch
  (incremental-aware) → write table with PK + `schema` (native/authoritative types) → persist
  `state.json`. Report objects branch to the reporting path (date-window + lookback).
- **`has_header` / `schema` interplay (silent Storage-load trap).** Under a `schema` (authoritative)
  manifest, `create_out_table_definition` defaults to `has_header=false`. If the CSV is written **with**
  a header row (nicer for debugging), pass `has_header=True` or Storage ingests the header text as a
  data row and the load fails. The write path and the manifest must agree — and this cannot be caught
  by local datadir/VCR tests (they diff files, not Storage typing), so it is verified on the Phase 7
  smoke run. Decision: write **headerless** CSVs (columns come from the `schema`) to sidestep it.
- **Error handling:** bad/expired credentials, `401`/`403`, invalid config, unknown object, exhausted
  retries → `UserException` (exit 1) with an actionable message. Unexpected errors bubble to exit 2.
- **Dependencies:** `keboola.component`, `requests`, `pydantic`, `tenacity` (or hand-rolled backoff).
  No SDK.

## 7. Testing

- **Datadir tests** (one dir per case): happy path per object family (campaigns, ad_groups, ads,
  report_campaign, a reference object); multi-page pagination (follows `next_url`); empty result;
  incremental (state in → filtered fetch → state advanced); auth failure `401` → exit 1; `429`
  retry-then-succeed.
- **VCR strategy:** record real interactions once the user provides credentials — one cassette per
  test case (token refresh + list/report calls). **Sanitizers** must scrub `#client_id`,
  `#client_secret`, `#refresh_token`, the `access_token` in the token response, and the
  `Authorization` header. Config fixtures use a single merged `config.json` (root params only) with
  row-scoped `state.json` — matching the platform's post-merge shape.
- **Sync-action tests:** `test-connection` success + failure; `list-ad-accounts` /
  `list-time-zones` return shaped dropdown options.
- **Seed payloads:** until live creds arrive, hand-author cassettes from the documented envelope
  (`{"data":[…],"pagination":{…}}`); re-record against the real account before the Phase 7 smoke test
  so report-body params are verified.

## 8. Deployment & validation (cf-dev)

- Build an `initial-implementation` branch image; register a config in **cf-dev** via `kbagent` with
  the image tag overridden via **`runtime.tag`** on the config (not by promoting the default tag).
- Create rows for a representative spread (e.g. campaigns + `report_campaign`) with the real
  `ad_account_id`; run a job.
- **Success looks like:** job exits 0; `campaigns.csv` and `report_campaign.csv` land in
  `in.c-keboola.ex-reddit-ads-<config>` with plausible row counts and the declared schema; the
  resolved image tag matches the branch build.

## 9. Open risks & blockers

Ranked; none structurally blocks starting implementation (contract items resolve via forked Postman /
one live call once creds land).

1. **Exact `Get A Report` request/response schema** (param names for level/metrics/breakdowns/date
   range; metric & breakdown enum values) — behind a doc URL blocked to research + sign-in-gated
   Postman. *Owner: implementer* — resolve via forked official Postman collection or first live call
   before finalizing the reporting client.
2. **Reports sync vs async** — inferred synchronous + paginated (no job/poll pattern seen); confirm on
   first live call. If async, add submit→poll→download to the client.
3. **Numeric rate limits per endpoint group** — documented but the table wasn't reachable; fetch
   before tuning backoff. Header-driven backoff is a safe default meanwhile.
4. **ID prefix convention** (`t2_`/`a2_`/others) — unconfirmed; don't hardcode prefix parsing, verify
   on live data.
5. **No sandbox** — all live testing uses a real account; use a low-spend/dedicated test account.
6. **Nested field flattening** decisions (targeting specs, creative details) — finalize from real
   payloads at implementation.
