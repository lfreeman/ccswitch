"""Tests for ccswitch.exec_runner."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from ccswitch.accounts import Account, AccountStore
from ccswitch import exec_runner
from ccswitch.exec_runner import (
    CCSWITCH_KEYCHAIN_SERVICE,
    TMPDIR_PREFIX,
    _extract_oauth_fields,
    run_with_account,
)


def _credential_blob(
    access: str = "access-tok",
    refresh: str = "refresh-tok",
    scopes: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": access,
                "refreshToken": refresh,
                "scopes": scopes if scopes is not None else ["scope-a", "scope-b"],
            }
        }
    )


@pytest.fixture
def with_store(tmp_home: Path) -> AccountStore:
    """Build an AccountStore at the default location with one saved account."""
    accounts_path = tmp_home / ".config" / "ccswitch" / "accounts.json"
    accounts_path.parent.mkdir(parents=True, exist_ok=True)
    store = AccountStore(accounts_path)
    store.add(
        Account(
            label="work",
            email="work@example.com",
            organization_name="Example Org",
            oauth_account={
                "accountUuid": "acc-1",
                "emailAddress": "work@example.com",
                "organizationUuid": "org-1",
                "organizationName": "Example Org",
            },
        )
    )
    return store


@pytest.fixture
def stub_keychain(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    entries: dict[tuple[str, str], str] = {}

    def fake_read(service: str, account: str) -> str | None:
        return entries.get((service, account))

    monkeypatch.setattr(exec_runner, "keychain_read", fake_read)
    return entries


# ---- _extract_oauth_fields ---------------------------------------------


def test_extract_oauth_fields_happy() -> None:
    access, refresh, scopes = _extract_oauth_fields(_credential_blob())
    assert access == "access-tok"
    assert refresh == "refresh-tok"
    assert scopes == "scope-a scope-b"


def test_extract_oauth_fields_rejects_bad_json() -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        _extract_oauth_fields("{not json")


def test_extract_oauth_fields_requires_claudeAiOauth() -> None:
    with pytest.raises(ValueError, match="claudeAiOauth"):
        _extract_oauth_fields(json.dumps({"other": {}}))


def test_extract_oauth_fields_requires_access_and_refresh() -> None:
    blob = json.dumps({"claudeAiOauth": {"accessToken": "a", "scopes": []}})
    with pytest.raises(ValueError, match="accessToken or refreshToken"):
        _extract_oauth_fields(blob)


def test_extract_oauth_fields_requires_scopes_list() -> None:
    blob = json.dumps(
        {"claudeAiOauth": {"accessToken": "a", "refreshToken": "r"}}
    )
    with pytest.raises(ValueError, match="scopes"):
        _extract_oauth_fields(blob)


def test_extract_oauth_fields_empty_scopes_list_ok() -> None:
    _, _, scopes = _extract_oauth_fields(_credential_blob(scopes=[]))
    assert scopes == ""


# ---- run_with_account: happy path --------------------------------------


def test_run_returns_zero_on_success(
    with_store: AccountStore, stub_keychain: dict
) -> None:
    stub_keychain[(CCSWITCH_KEYCHAIN_SERVICE, "work")] = _credential_blob()
    rc = run_with_account("work", ["true"])
    assert rc == 0


def test_run_propagates_nonzero_exit_code(
    with_store: AccountStore, stub_keychain: dict
) -> None:
    stub_keychain[(CCSWITCH_KEYCHAIN_SERVICE, "work")] = _credential_blob()
    rc = run_with_account("work", ["false"])
    assert rc != 0


def test_run_injects_all_four_env_vars(
    with_store: AccountStore, stub_keychain: dict, tmp_path: Path
) -> None:
    """AE3 + AE4: all three OAuth env vars + CLAUDE_CONFIG_DIR present in the child."""
    stub_keychain[(CCSWITCH_KEYCHAIN_SERVICE, "work")] = _credential_blob(
        access="ACCESS-X", refresh="REFRESH-X", scopes=["alpha", "beta"]
    )
    probe = tmp_path / "probe.json"
    script = (
        "import json, os\n"
        f"open({str(probe)!r}, 'w').write(json.dumps({{"
        "'token': os.environ.get('CLAUDE_CODE_OAUTH_TOKEN'),"
        "'refresh': os.environ.get('CLAUDE_CODE_OAUTH_REFRESH_TOKEN'),"
        "'scopes': os.environ.get('CLAUDE_CODE_OAUTH_SCOPES'),"
        "'config_dir': os.environ.get('CLAUDE_CONFIG_DIR'),"
        "}))\n"
    )
    rc = run_with_account("work", [sys.executable, "-c", script])
    assert rc == 0
    payload = json.loads(probe.read_text())
    assert payload["token"] == "ACCESS-X"
    assert payload["refresh"] == "REFRESH-X"
    assert payload["scopes"] == "alpha beta"
    assert payload["config_dir"].startswith("/")
    assert TMPDIR_PREFIX in payload["config_dir"]


def test_run_writes_scratch_claude_json(
    with_store: AccountStore, stub_keychain: dict, tmp_path: Path
) -> None:
    """The scratch CLAUDE_CONFIG_DIR contains a .claude.json with the target oauthAccount."""
    stub_keychain[(CCSWITCH_KEYCHAIN_SERVICE, "work")] = _credential_blob()
    probe = tmp_path / "probe.json"
    script = (
        "import json, os, pathlib\n"
        "cfg = pathlib.Path(os.environ['CLAUDE_CONFIG_DIR']) / '.claude.json'\n"
        f"open({str(probe)!r}, 'w').write(cfg.read_text())\n"
    )
    rc = run_with_account("work", [sys.executable, "-c", script])
    assert rc == 0
    written = json.loads(probe.read_text())
    assert written["oauthAccount"]["emailAddress"] == "work@example.com"
    assert written["oauthAccount"]["organizationUuid"] == "org-1"


def test_run_cleans_up_tmpdir_on_success(
    with_store: AccountStore, stub_keychain: dict, tmp_path: Path
) -> None:
    """AE5: scratch CLAUDE_CONFIG_DIR is removed after a successful run."""
    stub_keychain[(CCSWITCH_KEYCHAIN_SERVICE, "work")] = _credential_blob()
    probe = tmp_path / "cfgdir.txt"
    script = (
        "import os\n"
        f"open({str(probe)!r}, 'w').write(os.environ['CLAUDE_CONFIG_DIR'])\n"
    )
    rc = run_with_account("work", [sys.executable, "-c", script])
    assert rc == 0
    cfg_dir_used = probe.read_text()
    assert not Path(cfg_dir_used).exists()


def test_run_does_not_mutate_live_keychain_or_claude_json(
    tmp_home: Path,
    with_store: AccountStore,
    stub_keychain: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AE3: live Claude Code-credentials entry and live ~/.claude.json are unchanged after exec."""
    live_claude_json = tmp_home / ".claude.json"
    original_config = {"oauthAccount": {"emailAddress": "OLD"}, "projects": {"x": 1}}
    live_claude_json.write_text(json.dumps(original_config))

    # Add a fake "Claude Code-credentials" entry — we expect it untouched.
    stub_keychain[("Claude Code-credentials", os.environ.get("USER", "user"))] = "LIVE-BLOB"
    stub_keychain[(CCSWITCH_KEYCHAIN_SERVICE, "work")] = _credential_blob()

    rc = run_with_account("work", ["true"])
    assert rc == 0

    # Live state is unchanged.
    assert json.loads(live_claude_json.read_text()) == original_config
    assert stub_keychain[("Claude Code-credentials", os.environ.get("USER", "user"))] == "LIVE-BLOB"


# ---- run_with_account: error paths -------------------------------------


def test_run_missing_label_raises_before_any_subprocess(
    with_store: AccountStore, stub_keychain: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    def fake_run(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal called
        called = True
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(exec_runner.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="No saved account"):
        run_with_account("ghost", ["true"])
    assert called is False


def test_run_missing_keychain_entry_raises_before_any_subprocess(
    with_store: AccountStore, stub_keychain: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    def fake_run(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal called
        called = True
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(exec_runner.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="missing"):
        run_with_account("work", ["true"])
    assert called is False


def test_run_empty_argv_raises(with_store: AccountStore, stub_keychain: dict) -> None:
    stub_keychain[(CCSWITCH_KEYCHAIN_SERVICE, "work")] = _credential_blob()
    with pytest.raises(ValueError, match="command"):
        run_with_account("work", [])


def test_run_subprocess_exception_still_cleans_tmpdir(
    with_store: AccountStore, stub_keychain: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_keychain[(CCSWITCH_KEYCHAIN_SERVICE, "work")] = _credential_blob()

    seen_tmpdirs: list[str] = []

    real_mkdtemp = exec_runner.tempfile.mkdtemp

    def tracking_mkdtemp(**kw: Any) -> str:
        d = real_mkdtemp(**kw)
        seen_tmpdirs.append(d)
        return d

    monkeypatch.setattr(exec_runner.tempfile, "mkdtemp", tracking_mkdtemp)

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("spawn failed")

    monkeypatch.setattr(exec_runner.subprocess, "run", boom)

    with pytest.raises(OSError, match="spawn failed"):
        run_with_account("work", ["nope"])

    assert seen_tmpdirs, "expected the runner to create a tmpdir"
    for d in seen_tmpdirs:
        assert not Path(d).exists(), f"tmpdir {d} leaked on failure path"


def test_run_refreshes_expired_blob_before_injecting_env(
    with_store: AccountStore,
    stub_keychain: dict,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expired_blob = json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": "STALE",
                "refreshToken": "refresh-tok",
                "expiresAt": 1,
                "scopes": ["user:inference"],
            }
        }
    )
    refreshed_blob = json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": "FRESH",
                "refreshToken": "refresh-tok",
                "expiresAt": 99999999999999,
                "scopes": ["user:inference"],
            }
        }
    )
    stub_keychain[(CCSWITCH_KEYCHAIN_SERVICE, "work")] = expired_blob

    written: dict[tuple[str, str], str] = {}

    def fake_kc_write(service: str, account: str, value: str) -> None:
        written[(service, account)] = value
        stub_keychain[(service, account)] = value

    monkeypatch.setattr(exec_runner, "keychain_write", fake_kc_write)
    monkeypatch.setattr(exec_runner, "refresh_oauth_credentials", lambda _b: refreshed_blob)

    probe = tmp_path / "probe.json"
    script = (
        "import json, os\n"
        f"open({str(probe)!r}, 'w').write(json.dumps({{"
        "'token': os.environ.get('CLAUDE_CODE_OAUTH_TOKEN'),"
        "}))\n"
    )
    rc = run_with_account("work", [sys.executable, "-c", script])
    assert rc == 0
    assert json.loads(probe.read_text())["token"] == "FRESH"
    # Saved Keychain entry was updated with refreshed blob.
    assert written[(CCSWITCH_KEYCHAIN_SERVICE, "work")] == refreshed_blob


def test_run_skips_refresh_when_token_fresh(
    with_store: AccountStore,
    stub_keychain: dict,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from datetime import datetime, timezone
    future_ms = int(datetime.now(timezone.utc).timestamp() * 1000) + 60 * 60 * 1000
    fresh_blob = json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": "FRESH",
                "refreshToken": "refresh-tok",
                "expiresAt": future_ms,
                "scopes": ["user:inference"],
            }
        }
    )
    stub_keychain[(CCSWITCH_KEYCHAIN_SERVICE, "work")] = fresh_blob

    refresh_called = False

    def boom(_blob: str) -> None:
        nonlocal refresh_called
        refresh_called = True
        return None

    monkeypatch.setattr(exec_runner, "refresh_oauth_credentials", boom)

    rc = run_with_account("work", ["true"])
    assert rc == 0
    assert refresh_called is False


def test_run_falls_back_to_stale_blob_when_refresh_fails(
    with_store: AccountStore,
    stub_keychain: dict,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expired_blob = json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": "STALE",
                "refreshToken": "refresh-tok",
                "expiresAt": 1,
                "scopes": ["user:inference"],
            }
        }
    )
    stub_keychain[(CCSWITCH_KEYCHAIN_SERVICE, "work")] = expired_blob
    monkeypatch.setattr(exec_runner, "refresh_oauth_credentials", lambda _b: None)
    monkeypatch.setattr(exec_runner, "keychain_write", lambda *_a, **_kw: None)

    probe = tmp_path / "probe.json"
    script = (
        "import json, os\n"
        f"open({str(probe)!r}, 'w').write(json.dumps({{"
        "'token': os.environ.get('CLAUDE_CODE_OAUTH_TOKEN'),"
        "}))\n"
    )
    rc = run_with_account("work", [sys.executable, "-c", script])
    assert rc == 0
    # Child saw the stale token — Claude Code will surface its own 401 from there.
    assert json.loads(probe.read_text())["token"] == "STALE"


def test_run_stdio_passes_through(
    with_store: AccountStore, stub_keychain: dict, capfd: pytest.CaptureFixture
) -> None:
    stub_keychain[(CCSWITCH_KEYCHAIN_SERVICE, "work")] = _credential_blob()
    rc = run_with_account("work", [sys.executable, "-c", "print('hello-from-child')"])
    assert rc == 0
    captured = capfd.readouterr()
    assert "hello-from-child" in captured.out
