#!/usr/bin/env python3
"""One-time helper to mint a Reddit Ads OAuth refresh token.

Reddit Ads uses the OAuth 2.0 authorization-code grant, which requires a single
human browser consent. This script drives that flow locally: it opens the
authorize page in your browser, captures the redirect on a small local HTTP
listener, exchanges the code, and prints the ``refresh_token`` to paste into the
Keboola component configuration.

This is a DEV/SETUP convenience only — it is NOT part of the component runtime.

Prerequisites
-------------
- A Reddit Developer Application (Business → Developer Portal) with the redirect
  URI set to exactly the ``--redirect-uri`` you pass here (default
  ``http://localhost:8080``).
- ``pip install requests`` (already a component dependency).

Usage
-----
    python scripts/get_refresh_token.py \
        --client-id YOUR_CLIENT_ID \
        --client-secret YOUR_CLIENT_SECRET

Credentials can also be supplied via the ``REDDIT_CLIENT_ID`` /
``REDDIT_CLIENT_SECRET`` environment variables.
"""

from __future__ import annotations

import argparse
import http.server
import os
import secrets
import sys
import threading
import urllib.parse
import webbrowser

import requests

AUTHORIZE_URL = "https://www.reddit.com/api/v1/authorize"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
USER_AGENT = "keboola-ex-reddit-ads-setup/1.0"
DEFAULT_SCOPE = "adsread"


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Captures the ?code=... redirect on a single request, then stops."""

    captured: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        query = urllib.parse.urlparse(self.path).query
        params = dict(urllib.parse.parse_qsl(query))
        _CallbackHandler.captured = params
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = "Authorization received — you can close this tab and return to the terminal."
        if "error" in params:
            msg = f"Authorization failed: {params['error']}. Check the terminal."
        self.wfile.write(f"<html><body><h3>{msg}</h3></body></html>".encode())

    def log_message(self, *_args: object) -> None:
        pass  # silence the default request logging


def _run_listener(host: str, port: int) -> http.server.HTTPServer:
    server = http.server.HTTPServer((host, port), _CallbackHandler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description="Mint a Reddit Ads OAuth refresh token.")
    parser.add_argument("--client-id", default=os.environ.get("REDDIT_CLIENT_ID"))
    parser.add_argument("--client-secret", default=os.environ.get("REDDIT_CLIENT_SECRET"))
    parser.add_argument("--redirect-uri", default="http://localhost:8080")
    parser.add_argument("--scope", default=DEFAULT_SCOPE)
    args = parser.parse_args()

    if not args.client_id or not args.client_secret:
        parser.error("client id and secret are required (flags or REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET).")

    parsed = urllib.parse.urlparse(args.redirect_uri)
    host, port = parsed.hostname or "localhost", parsed.port or 8080

    state = secrets.token_urlsafe(16)
    authorize_url = f"{AUTHORIZE_URL}?" + urllib.parse.urlencode(
        {
            "client_id": args.client_id,
            "response_type": "code",
            "state": state,
            "redirect_uri": args.redirect_uri,
            "duration": "permanent",  # REQUIRED to receive a refresh token
            "scope": args.scope,
        }
    )

    server = _run_listener(host, port)
    print(f"Opening the browser to authorize (listening on {args.redirect_uri}) …")
    print(f"If the browser doesn't open, visit:\n{authorize_url}\n")
    webbrowser.open(authorize_url)

    # handle_request() runs on a daemon thread; wait for it to capture the redirect.
    server.serve_forever_timeout = 300
    threading.Event().wait(0.1)
    print("Waiting for authorization (up to 5 minutes) …")
    for _ in range(300):
        if _CallbackHandler.captured:
            break
        threading.Event().wait(1)
    captured = _CallbackHandler.captured
    server.server_close()

    if not captured or "code" not in captured:
        print(f"No authorization code received. Response: {captured or 'none'}", file=sys.stderr)
        return 1
    if captured.get("state") != state:
        print("State mismatch — aborting for safety.", file=sys.stderr)
        return 1

    resp = requests.post(
        TOKEN_URL,
        auth=(args.client_id, args.client_secret),
        data={
            "grant_type": "authorization_code",
            "code": captured["code"],
            "redirect_uri": args.redirect_uri,
        },
        headers={"User-Agent": USER_AGENT},
        timeout=60,
    )
    if not resp.ok:
        print(f"Token exchange failed (HTTP {resp.status_code}): {resp.text[:500]}", file=sys.stderr)
        return 1

    refresh_token = (resp.json() or {}).get("refresh_token")
    if not refresh_token:
        print(
            "No refresh_token in the response. Make sure the app used duration=permanent and the "
            f"scope '{args.scope}' was granted. Response: {resp.text[:500]}",
            file=sys.stderr,
        )
        return 1

    print("\n" + "=" * 60)
    print("SUCCESS — paste this into the component's Refresh Token field:")
    print(refresh_token)
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
