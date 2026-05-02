"""Interactive OAuth 2.0 authorization-code flow for the Schwab API.

Run via ``traider auth schwab``. Writes the resulting access/refresh
tokens to the token file consumed by ``SchwabClient``.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
from pathlib import Path

import httpx

from .schwab_client import (
    DEFAULT_TOKEN_FILE,
    SCHWAB_AUTHORIZE_URL,
    SCHWAB_TOKEN_URL,
)

logger = logging.getLogger("traider.schwab.auth")


def run_auth_flow() -> None:
    app_key = os.environ.get("SCHWAB_APP_KEY")
    app_secret = os.environ.get("SCHWAB_APP_SECRET")
    callback_url = os.environ.get("SCHWAB_CALLBACK_URL")
    missing = [
        name
        for name, value in [
            ("SCHWAB_APP_KEY", app_key),
            ("SCHWAB_APP_SECRET", app_secret),
            ("SCHWAB_CALLBACK_URL", callback_url),
        ]
        if not value
    ]
    if missing:
        raise SystemExit(f"Missing env vars: {', '.join(missing)}")

    token_file = Path(
        os.environ.get("SCHWAB_TOKEN_FILE", str(DEFAULT_TOKEN_FILE))
    )

    authorize_url = SCHWAB_AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": app_key,
        "redirect_uri": callback_url,
    })

    print("1. Open this URL in a browser and log in to Schwab:")
    print()
    print(f"   {authorize_url}")
    print()
    print("2. After authorizing, the browser will be redirected to your")
    print("   callback URL. It will probably show an error page — that's")
    print("   expected. Copy the FULL redirected URL from the address bar.")
    print()
    redirected = input("Paste the redirected URL: ").strip()

    parsed = urllib.parse.urlparse(redirected)
    code_values = urllib.parse.parse_qs(parsed.query).get("code", [])
    code = code_values[0] if code_values else None
    if not code:
        raise SystemExit(
            "No 'code' query parameter found in the pasted URL."
        )

    with httpx.Client(timeout=10.0) as client:
        r = client.post(
            SCHWAB_TOKEN_URL,
            auth=(app_key, app_secret),
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": callback_url,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    if r.status_code != 200:
        raise SystemExit(
            f"Token exchange failed: status={r.status_code} body={r.text[:500]}"
        )

    body = r.json()
    tokens = {
        "access_token": body["access_token"],
        "refresh_token": body["refresh_token"],
        "expires_at": time.time() + int(body.get("expires_in", 1800)),
        "token_type": body.get("token_type", "Bearer"),
    }

    token_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = token_file.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(tokens, f, indent=2)
    os.replace(tmp, token_file)
    try:
        os.chmod(token_file, 0o600)
    except OSError:
        # Best-effort; Windows filesystems may reject chmod.
        pass

    print()
    print(f"Tokens written to {token_file}")
