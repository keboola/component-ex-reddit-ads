### Configuration

**Connection (config level):**
- **Client ID / Client Secret** — from your Reddit Developer Application (Business → Developer Portal).
- **Refresh Token** — minted once via the one-time browser authorization (see documentation).
- **Test Connection** — validates the credentials.
- **Business / Ad Account** — the Reddit Ads business and account to extract from (loaded via the credentials).

**Per-row settings:**
- **Object to extract** — one Reddit Ads object per row. Choose *Report* for performance metrics.
- **Load type** — full load or incremental upsert (reference objects are always full-loaded).
- **Report settings** (shown when the object is *Report*) — metrics, up to 3 breakdowns, and a
  last-N-days or custom date range with a lookback window.
