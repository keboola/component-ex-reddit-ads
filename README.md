ex-reddit-ads
=============

Extracts advertising structure and performance data from the
[Reddit Ads API v3](https://ads-api.reddit.com/docs/v3/) into Keboola Storage.

**Table of Contents:**

[TOC]

Functionality Notes
===================

The component extracts the full Reddit Ads read surface — **one object per configuration row**:

- **Structure:** ad accounts, businesses, members, ad account history, campaigns, ad groups, ads, posts.
- **Audiences & targeting:** custom audiences, saved audiences, targeting options.
- **Measurement & assets:** pixels, lead generation forms, product catalogs, SKAdNetwork, apps, profiles.
- **Billing & reference:** funding instruments, time zones.
- **Reports:** performance metrics at account / campaign / ad group / ad level, with up to three
  breakdowns, configurable time granularity, and a last-N-days or custom date range.

Entity objects support incremental loading via a `last_run` watermark (upsert on primary key).
Reports are re-pulled over the date window with a configurable lookback so late conversion
attribution is captured.

Prerequisites — Authentication
==============================

Reddit Ads uses **OAuth 2.0 (authorization-code grant)**. `client_id` + `client_secret` alone are not
enough — a one-time browser consent is required to mint a **refresh token**. After that, the component
refreshes access tokens automatically on every run (fully headless).

### 1. Create a Reddit Developer Application

1. Sign in at [ads.reddit.com](https://ads.reddit.com) with a **Business Admin** account.
2. Go to **Business → Developer Portal → Add Apps → Create an app**.
3. Set:
   - **Redirect URI:** `http://localhost:8080` (to use the helper script below) — or
     `https://oauth.pstmn.io/v1/callback` (to use Postman).
   - **Primary contact:** a business admin.
4. Save. You get an **App ID (`client_id`)** and **secret (`client_secret`)**.

### 2. Mint a refresh token (choose one)

**Option A — helper script (recommended, no extra tools):**

```
python scripts/get_refresh_token.py --client-id YOUR_CLIENT_ID --client-secret YOUR_CLIENT_SECRET
```

It opens the browser, captures the redirect on a local listener, exchanges the code, and prints the
`refresh_token`. Requires the app's redirect URI to be `http://localhost:8080` (or pass
`--redirect-uri`).

**Option B — Postman:** On a request's **Authorization** tab, choose **OAuth 2.0**, Grant Type
*Authorization Code*, Auth URL `https://www.reddit.com/api/v1/authorize?duration=permanent`, Access
Token URL `https://www.reddit.com/api/v1/access_token`, Callback URL
`https://oauth.pstmn.io/v1/callback`, Scope `adsread`, **Client Authentication = Send as Basic Auth
header**. Click *Get New Access Token* and copy the `refresh_token`.

**Option C — curl:** Open
`https://www.reddit.com/api/v1/authorize?client_id=CLIENT_ID&response_type=code&state=x&redirect_uri=REDIRECT&duration=permanent&scope=adsread`
in a browser, approve, copy the `code` from the redirect, then:

```
curl -X POST https://www.reddit.com/api/v1/access_token \
  -u "CLIENT_ID:CLIENT_SECRET" \
  -A "keboola-ex-reddit-ads-setup/1.0" \
  -d grant_type=authorization_code -d code=THE_CODE -d redirect_uri=REDIRECT
```

The response contains the `refresh_token`.

### 3. Configure the component

Enter **Client ID**, **Client Secret**, **Refresh Token**, click **Test Connection**, and select an
**Ad Account**. Then add rows for the objects/reports to extract.

Configuration
=============

**Connection (config level):** Client ID, Client Secret, Refresh Token, Ad Account.

**Per-row settings:** Object to extract; Load type (full / incremental); Report settings (level,
metrics, up to 3 breakdowns, time zone, granularity, date range + lookback) shown when the object is
*Report*.

Output
======

One table per object (e.g. `campaigns`, `ads`, `report_campaign`), written to the component's default
bucket. Entity tables key on `id`; report tables key on date + entity id + breakdown columns.

Development
-----------

To customize the local data folder path, replace the `CUSTOM_FOLDER` placeholder with your desired path in the `docker-compose.yml` file:

~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    volumes:
      - ./:/code
      - ./CUSTOM_FOLDER:/data
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Clone this repository, initialize the workspace, and run the component using the following
commands:

~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
git clone  component-ex-reddit-ads
cd component-ex-reddit-ads
docker-compose build
docker-compose run --rm dev
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Run the test suite and perform lint checks using this command:

~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
docker-compose run --rm test
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Integration
===========

For details about deployment and integration with Keboola, refer to the
[deployment section of the developer
documentation](https://developers.keboola.com/extend/component/deployment/).
