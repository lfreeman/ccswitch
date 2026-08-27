"""Per-command execution with env injection + ephemeral CLAUDE_CONFIG_DIR.

``run_with_account(label, cmd_argv)`` runs a one-shot command under a
specific saved account without disturbing the globally-active Claude
Code login. Credentials come from one of two sources, in order: a
long-lived ``claude setup-token`` saved via ``ccswitch add-token``
(injected as ``CLAUDE_CODE_OAUTH_TOKEN`` — preferred, since it does not
share the interactive login's rotating refresh chain), or the saved
browser-login blob (injected as the three OAuth env vars, refreshing an
expired access token first). Either way the child also gets a scratch
``CLAUDE_CONFIG_DIR`` holding a minimal ``.claude.json``, removed on
every exit path.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from ccswitch.accounts import AccountStore
from ccswitch.jsonio import write_json_atomic
from ccswitch.keychain import read as keychain_read
from ccswitch.keychain import write as keychain_write
from ccswitch.oauth import (
    extract_oauth_data,
    is_oauth_token_expired,
    refresh_oauth_credentials,
)

CCSWITCH_KEYCHAIN_SERVICE = "ccswitch"
SETUP_TOKEN_SERVICE = "ccswitch-token"
TMPDIR_PREFIX = "ccswitch-"

# The three OAuth env vars Claude Code reads. We always clear inherited copies
# before injecting our own so a stale value from the parent shell can't leak
# into the child (Claude Code rejects a refresh token without matching scopes).
_OAUTH_ENV_VARS = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
    "CLAUDE_CODE_OAUTH_SCOPES",
)


def _extract_oauth_fields(blob: str) -> tuple[str, str, str]:
    """Return ``(accessToken, refreshToken, scopes_space_joined)`` from the blob.

    The ``claude`` binary requires all three env vars when
    ``CLAUDE_CODE_OAUTH_REFRESH_TOKEN`` is set, so this function refuses any
    blob missing one of them.
    """
    try:
        data = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Saved credential blob is not valid JSON: {exc.msg}.") from exc
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        raise ValueError(
            "Saved credential blob has no 'claudeAiOauth' section; "
            "re-run `ccswitch add` to repair it."
        )
    access = oauth.get("accessToken")
    refresh = oauth.get("refreshToken")
    scopes = oauth.get("scopes")
    if not isinstance(access, str) or not isinstance(refresh, str):
        raise ValueError(
            "Saved credential blob is missing accessToken or refreshToken; "
            "re-run `ccswitch add` to repair it."
        )
    if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
        raise ValueError(
            "Saved credential blob is missing 'scopes' (required when a refresh "
            "token is present); re-run `ccswitch add` to repair it."
        )
    return access, refresh, " ".join(scopes)


def _refresh_or_raise(label: str, blob: str) -> str:
    """Return a blob with a non-expired access token, refreshing if needed.

    Refreshes when the stored access token is expired and persists the result
    back to the per-label Keychain entry. Raises ``ValueError`` when the token
    is expired and refresh fails — the common case, since the stored refresh
    token is rotated out whenever the same account refreshes live in Claude
    Code. Injecting the dead token instead would only yield an opaque 401 from
    the child, so we fail loudly and point at the recovery path.
    """
    oauth = extract_oauth_data(blob)
    if not oauth or not is_oauth_token_expired(oauth.get("expiresAt")):
        return blob
    refreshed = refresh_oauth_credentials(blob)
    if refreshed is None:
        raise ValueError(
            f"Saved credentials for {label!r} are expired and could not be "
            f"refreshed — the stored refresh token was rotated out or revoked "
            f"(this happens whenever the same account refreshes live in Claude "
            f"Code). Capture a long-lived token instead: run `claude setup-token` "
            f"while logged into that account, then `ccswitch add-token {label}`. "
            f"Alternatively, re-login and re-run `ccswitch add --label {label}`."
        )
    keychain_write(CCSWITCH_KEYCHAIN_SERVICE, label, refreshed)
    return refreshed


def run_with_account(label: str, cmd_argv: list[str]) -> int:
    """Run ``cmd_argv`` as a child process scoped to the saved account ``label``.

    Returns the child's exit code. Raises ``ValueError`` if the label is
    unknown or its stored credentials are unusable — in both cases the
    global state and the temp dir are untouched.
    """
    if not cmd_argv:
        raise ValueError("exec requires a command to run after the label.")

    store = AccountStore()
    account = store.get(label)
    if account is None:
        raise ValueError(
            f"No saved account labeled {label!r}. Run `ccswitch list` to see saved labels."
        )

    # A long-lived `claude setup-token` (stored via `ccswitch add-token`) is
    # preferred: it is independent of the interactive login's rotating refresh
    # chain, so it survives the live Claude Code app refreshing the same
    # account. Fall back to the saved browser-login blob when no token exists.
    #
    # Caveat on the blob path: the child is handed a refresh token, so a
    # long-running command that outlives the access token makes Claude Code
    # refresh in-process. That rotation invalidates the refresh token
    # server-side, but the new tokens land in the scratch CLAUDE_CONFIG_DIR we
    # delete on exit, leaving the saved and live copies stale. The setup-token
    # path carries no refresh token and is unaffected.
    setup_token = keychain_read(SETUP_TOKEN_SERVICE, label)
    if setup_token:
        oauth_env = {"CLAUDE_CODE_OAUTH_TOKEN": setup_token}
    else:
        blob = keychain_read(CCSWITCH_KEYCHAIN_SERVICE, label)
        if not blob:
            raise ValueError(
                f"Saved Keychain entry for label {label!r} is missing. "
                f"Re-run `ccswitch add` to repair it."
            )
        blob = _refresh_or_raise(label, blob)
        access_token, refresh_token, scopes = _extract_oauth_fields(blob)
        oauth_env = {
            "CLAUDE_CODE_OAUTH_TOKEN": access_token,
            "CLAUDE_CODE_OAUTH_REFRESH_TOKEN": refresh_token,
            "CLAUDE_CODE_OAUTH_SCOPES": scopes,
        }

    tmpdir = tempfile.mkdtemp(prefix=TMPDIR_PREFIX)
    try:
        scratch_config = Path(tmpdir) / ".claude.json"
        write_json_atomic(scratch_config, {"oauthAccount": account.oauth_account})

        env = os.environ.copy()
        for var in _OAUTH_ENV_VARS:
            env.pop(var, None)
        env.update(oauth_env)
        env["CLAUDE_CONFIG_DIR"] = tmpdir

        result = subprocess.run(cmd_argv, env=env)
        return result.returncode
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
