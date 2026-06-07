"""Per-command execution with env injection + ephemeral CLAUDE_CONFIG_DIR.

``run_with_account(label, cmd_argv)`` runs a one-shot command under a
specific saved account without disturbing the globally-active Claude
Code login. It hands the child the four env vars Claude Code reads for
non-interactive operation (the three OAuth fields plus a scratch
``CLAUDE_CONFIG_DIR`` that holds a minimal ``.claude.json``) and cleans
up the scratch directory on every exit path.
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
TMPDIR_PREFIX = "ccswitch-"


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


def _refresh_if_expired(label: str, blob: str) -> str:
    """Refresh the access token in ``blob`` when expired; persist back to Keychain.

    Best-effort: on refresh failure, returns the original blob unchanged. The
    child process will then surface its own auth error from the API. We do
    not warn on stderr here so that ``exec`` stays quiet on the happy path
    when the cache is already fresh; the child is the appropriate channel
    for auth failures.
    """
    oauth = extract_oauth_data(blob)
    if not oauth or not is_oauth_token_expired(oauth.get("expiresAt")):
        return blob
    refreshed = refresh_oauth_credentials(blob)
    if refreshed is None:
        return blob
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

    blob = keychain_read(CCSWITCH_KEYCHAIN_SERVICE, label)
    if not blob:
        raise ValueError(
            f"Saved Keychain entry for label {label!r} is missing. "
            f"Re-run `ccswitch add` to repair it."
        )
    blob = _refresh_if_expired(label, blob)
    access_token, refresh_token, scopes = _extract_oauth_fields(blob)

    tmpdir = tempfile.mkdtemp(prefix=TMPDIR_PREFIX)
    try:
        scratch_config = Path(tmpdir) / ".claude.json"
        write_json_atomic(scratch_config, {"oauthAccount": account.oauth_account})

        env = os.environ.copy()
        env["CLAUDE_CODE_OAUTH_TOKEN"] = access_token
        env["CLAUDE_CODE_OAUTH_REFRESH_TOKEN"] = refresh_token
        env["CLAUDE_CODE_OAUTH_SCOPES"] = scopes
        env["CLAUDE_CONFIG_DIR"] = tmpdir

        result = subprocess.run(cmd_argv, env=env)
        return result.returncode
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
