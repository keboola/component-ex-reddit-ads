The Reddit Ads extractor pulls advertising data from the [Reddit Ads API v3](https://ads-api.reddit.com/docs/v3/)
into Keboola Storage for marketing analytics and cross-channel reporting.

It covers the full Reddit Ads read surface — one object per configuration row:

- **Structure:** ad accounts, businesses, members, ad account history, campaigns, ad groups, ads, posts.
- **Audiences & targeting:** custom audiences, saved audiences, targeting options.
- **Measurement & assets:** pixels, lead generation forms, product catalogs, SKAdNetwork, apps, profiles.
- **Billing & reference:** funding instruments, time zones.
- **Performance reports:** metrics at account / campaign / ad group / ad level, with up to three
  breakdowns and configurable time granularity and date range.

Authentication uses OAuth 2.0. You create a Reddit Developer Application (Client ID + Secret) and
authorize it once to obtain a refresh token; the component then refreshes access tokens automatically
on every run. Entity objects support incremental loading via a `last_run` watermark, and reports are
re-pulled over a configurable date window with a lookback to capture late conversion attribution.
