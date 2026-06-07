"""OAuth helpers: extract fields, detect expiry, refresh access tokens.

Claude Code's saved access tokens expire on the order of an hour; when
ccswitch restores a stale blob into the live Keychain entry, Claude Code
doesn't refresh from a freshly-restored entry, so a switch to a long-saved
account surfaces as ``API Error: 401`` until the user re-logs-in.

:func:`refresh_oauth_credentials` works around that by minting a fresh
access token directly against ``platform.claude.com/v1/oauth/token`` using
the saved refresh token. The endpoint, client ID, request shape, and beta
header here are the same ones the ``claude`` binary itself uses (verified
by inspecting its bundled JS); if the refresh succeeds, callers should
persist the returned blob both back to its per-label store and into the
live ``Claude Code-credentials`` Keychain entry so subsequent operations
don't refresh again until the new token expires.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
# Refresh slightly ahead of the wire-reported expiry to absorb clock skew and
# request latency.
OAUTH_EXPIRY_BUFFER_MS = 5 * 60 * 1000
REFRESH_TIMEOUT_SECONDS = 10


def extract_oauth_data(credentials: str) -> dict[str, Any] | None:
    """Return the ``claudeAiOauth`` subtree from a credentials blob, or ``None``."""
    try:
        data = json.loads(credentials)
    except json.JSONDecodeError:
        return None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    return oauth if isinstance(oauth, dict) else None


def is_oauth_token_expired(expires_at: object) -> bool:
    """Return ``True`` when ``expires_at`` (ms since epoch) is past or about to pass."""
    if not isinstance(expires_at, (int, float)):
        return False
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return now_ms + OAUTH_EXPIRY_BUFFER_MS >= int(expires_at)


def refresh_oauth_credentials(credentials: str) -> str | None:
    """Refresh the access token via the OAuth endpoint; return the updated blob.

    Returns ``None`` on any failure (network error, invalid_grant, malformed
    response, missing refresh token). Callers should fall back to writing the
    original ``credentials`` blob and surface a hint about re-login + re-add.
    """
    try:
        data = json.loads(credentials)
    except json.JSONDecodeError:
        return None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        return None
    refresh_token = oauth.get("refreshToken")
    if not isinstance(refresh_token, str) or not refresh_token:
        return None

    body = json.dumps(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": OAUTH_CLIENT_ID,
        }
    ).encode()
    req = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "ccswitch/0.1"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=REFRESH_TIMEOUT_SECONDS) as resp:
            resp_data = json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError):
        return None

    access = resp_data.get("access_token")
    expires_in = resp_data.get("expires_in")
    if not isinstance(access, str) or not isinstance(expires_in, (int, float)):
        return None

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    oauth["accessToken"] = access
    oauth["expiresAt"] = now_ms + int(expires_in) * 1000
    new_refresh = resp_data.get("refresh_token")
    if isinstance(new_refresh, str) and new_refresh:
        oauth["refreshToken"] = new_refresh
    scope_str = resp_data.get("scope")
    if isinstance(scope_str, str) and scope_str:
        oauth["scopes"] = scope_str.split()

    data["claudeAiOauth"] = oauth
    return json.dumps(data)
