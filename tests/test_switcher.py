"""Tests for ccswitch.switcher.

Reads and writes against the live Keychain are mocked by patching the
``keychain_read`` / ``keychain_write`` / ``keychain_delete`` names that
``switcher`` imported, so no test touches the real Keychain.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from ccswitch.accounts import Account, AccountStore
from ccswitch import switcher as switcher_mod
from ccswitch.keychain import KeychainError
from ccswitch.switcher import (
    CCSWITCH_KEYCHAIN_SERVICE,
    CLAUDE_CREDENTIALS_SERVICE,
    Switcher,
)


# ---- Fakes / fixtures ---------------------------------------------------


class FakeKeychain:
    """In-memory ``(service, account) -> value`` store."""

    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], str] = {}

    def read(self, service: str, account: str) -> str | None:
        return self.entries.get((service, account))

    def write(self, service: str, account: str, value: str) -> None:
        self.entries[(service, account)] = value

    def delete(self, service: str, account: str) -> None:
        self.entries.pop((service, account), None)


@pytest.fixture
def fake_kc(monkeypatch: pytest.MonkeyPatch) -> FakeKeychain:
    kc = FakeKeychain()
    monkeypatch.setattr(switcher_mod, "keychain_read", kc.read)
    monkeypatch.setattr(switcher_mod, "keychain_write", kc.write)
    monkeypatch.setattr(switcher_mod, "keychain_delete", kc.delete)
    monkeypatch.setenv("USER", "tester")
    return kc


@pytest.fixture
def claude_config_path(tmp_home: Path) -> Path:
    return tmp_home / ".claude.json"


def _write_claude_config(path: Path, oauth: dict | None, extra: dict | None = None) -> None:
    data: dict[str, Any] = {
        "userID": "user-xyz",
        "projects": {"/Users/leo/code": {"sessions": ["sess-1"]}},
        "version": "1.0.0",
    }
    if extra:
        data.update(extra)
    if oauth is not None:
        data["oauthAccount"] = oauth
    path.write_text(json.dumps(data, indent=2))


def _oauth(email: str, org_uuid: str, org_name: str = "Org") -> dict:
    return {
        "accountUuid": "acc-" + org_uuid,
        "emailAddress": email,
        "organizationUuid": org_uuid,
        "organizationName": org_name,
    }


def _make_switcher(
    tmp_home: Path,
    console: Console | None = None,
) -> tuple[Switcher, AccountStore]:
    store_path = tmp_home / ".config" / "ccswitch" / "accounts.json"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store = AccountStore(store_path)
    sw = Switcher(store=store, console=console or Console(record=True, force_terminal=False))
    return sw, store


def _record_console() -> Console:
    return Console(record=True, force_terminal=False, width=200)


# ---- add ----------------------------------------------------------------


def test_add_saves_active_account_default_label(
    tmp_home: Path, fake_kc: FakeKeychain, claude_config_path: Path
) -> None:
    _write_claude_config(claude_config_path, _oauth("work@example.com", "org-1", "Example Org"))
    fake_kc.write(CLAUDE_CREDENTIALS_SERVICE, "tester", '{"claudeAiOauth": {"x": 1}}')

    sw, store = _make_switcher(tmp_home)

    # Force the prompt to return the suggested default.
    captured = {}

    def fake_prompt(message: str, default: str | None = None) -> str:
        captured["default"] = default
        return default or "fallback"

    sw._prompt_text = fake_prompt  # type: ignore[assignment]

    sw.add()

    assert captured["default"] == "work-example-org"
    saved = store.get("work-example-org")
    assert saved is not None
    assert saved.email == "work@example.com"
    assert saved.organization_name == "Example Org"
    assert fake_kc.entries[(CCSWITCH_KEYCHAIN_SERVICE, "work-example-org")] == '{"claudeAiOauth": {"x": 1}}'


def test_add_with_label_override_skips_prompt(
    tmp_home: Path, fake_kc: FakeKeychain, claude_config_path: Path
) -> None:
    _write_claude_config(claude_config_path, _oauth("work@example.com", "org-1", "Example Org"))
    fake_kc.write(CLAUDE_CREDENTIALS_SERVICE, "tester", "blob")

    sw, store = _make_switcher(tmp_home)
    sw._prompt_text = lambda *_args, **_kwargs: pytest.fail("prompt should not be called")  # type: ignore[assignment]

    sw.add(label_override="custom-label")

    assert store.get("custom-label") is not None


def test_add_same_identity_existing_prompts_to_update(
    tmp_home: Path, fake_kc: FakeKeychain, claude_config_path: Path
) -> None:
    oauth = _oauth("work@example.com", "org-1", "Example Org")
    _write_claude_config(claude_config_path, oauth)
    fake_kc.write(CLAUDE_CREDENTIALS_SERVICE, "tester", "new-blob")

    sw, store = _make_switcher(tmp_home)
    store.add(Account("work", "work@example.com", "Example Org", oauth))
    fake_kc.write(CCSWITCH_KEYCHAIN_SERVICE, "work", "old-blob")

    sw._confirm = lambda *_a, **_kw: True  # type: ignore[assignment]
    sw.add(label_override="work")
    assert fake_kc.entries[(CCSWITCH_KEYCHAIN_SERVICE, "work")] == "new-blob"


def test_add_different_identity_existing_label_raises(
    tmp_home: Path, fake_kc: FakeKeychain, claude_config_path: Path
) -> None:
    _write_claude_config(claude_config_path, _oauth("work@example.com", "org-1", "Example Org"))
    fake_kc.write(CLAUDE_CREDENTIALS_SERVICE, "tester", "blob")

    sw, store = _make_switcher(tmp_home)
    # Existing entry under same label but for a *different* identity.
    store.add(Account("collision", "other@x.com", "Other", _oauth("other@x.com", "org-2", "Other")))

    with pytest.raises(ValueError, match="already in use"):
        sw.add(label_override="collision")


def test_add_raises_when_no_oauth_account(
    tmp_home: Path, fake_kc: FakeKeychain, claude_config_path: Path
) -> None:
    _write_claude_config(claude_config_path, None)
    sw, _ = _make_switcher(tmp_home)
    with pytest.raises(ValueError, match="no oauthAccount"):
        sw.add(label_override="x")


def test_add_raises_when_no_active_credentials(
    tmp_home: Path, fake_kc: FakeKeychain, claude_config_path: Path
) -> None:
    _write_claude_config(claude_config_path, _oauth("work@example.com", "org-1"))
    sw, _ = _make_switcher(tmp_home)
    with pytest.raises(ValueError, match="No active Claude Code credentials"):
        sw.add(label_override="x")


def test_add_bad_label_raises(
    tmp_home: Path, fake_kc: FakeKeychain, claude_config_path: Path
) -> None:
    _write_claude_config(claude_config_path, _oauth("work@example.com", "org-1"))
    fake_kc.write(CLAUDE_CREDENTIALS_SERVICE, "tester", "blob")

    sw, _ = _make_switcher(tmp_home)
    with pytest.raises(ValueError):
        sw.add(label_override="bad label")


# ---- list_accounts ------------------------------------------------------


def test_list_empty_store(
    tmp_home: Path, fake_kc: FakeKeychain
) -> None:
    console = _record_console()
    sw, _ = _make_switcher(tmp_home, console=console)
    sw.list_accounts()
    out = console.export_text()
    assert "No saved accounts" in out


def test_list_marks_active(
    tmp_home: Path, fake_kc: FakeKeychain, claude_config_path: Path
) -> None:
    active_oauth = _oauth("work@example.com", "org-1", "Example Org")
    _write_claude_config(claude_config_path, active_oauth)

    console = _record_console()
    sw, store = _make_switcher(tmp_home, console=console)
    store.add(Account("work", "work@example.com", "Example Org", active_oauth))
    store.add(Account("personal", "personal@example.com", "", _oauth("personal@example.com", "org-9", "")))

    sw.list_accounts()
    out = console.export_text()
    assert "work" in out
    assert "personal" in out
    # Active marker appears on the active row (●).
    lines = out.splitlines()
    leo_bg_line = next(line for line in lines if "work" in line)
    personal_line = next(line for line in lines if "personal" in line)
    assert "●" in leo_bg_line
    assert "●" not in personal_line


def test_list_no_claude_config_still_renders(
    tmp_home: Path, fake_kc: FakeKeychain
) -> None:
    # No .claude.json at all → table renders without an active marker.
    console = _record_console()
    sw, store = _make_switcher(tmp_home, console=console)
    store.add(Account("work", "work@example.com", "Example Org", _oauth("work@example.com", "org-1")))
    sw.list_accounts()
    out = console.export_text()
    assert "work" in out
    assert "●" not in out


# ---- current ------------------------------------------------------------


def test_current_active_saved(
    tmp_home: Path, fake_kc: FakeKeychain, claude_config_path: Path
) -> None:
    oauth = _oauth("work@example.com", "org-1", "Example Org")
    _write_claude_config(claude_config_path, oauth)

    console = _record_console()
    sw, store = _make_switcher(tmp_home, console=console)
    store.add(Account("work", "work@example.com", "Example Org", oauth))

    sw.current()
    out = console.export_text()
    assert "work" in out
    assert "work@example.com" in out


def test_current_active_not_saved(
    tmp_home: Path, fake_kc: FakeKeychain, claude_config_path: Path
) -> None:
    _write_claude_config(claude_config_path, _oauth("work@example.com", "org-1", "Example Org"))
    console = _record_console()
    sw, _ = _make_switcher(tmp_home, console=console)
    sw.current()
    out = console.export_text()
    assert "work@example.com" in out
    assert "not saved" in out


def test_current_no_oauth(
    tmp_home: Path, fake_kc: FakeKeychain, claude_config_path: Path
) -> None:
    _write_claude_config(claude_config_path, None)
    console = _record_console()
    sw, _ = _make_switcher(tmp_home, console=console)
    sw.current()
    out = console.export_text()
    assert "No active Claude account" in out


# ---- use ----------------------------------------------------------------


def test_use_happy_path_preserves_other_fields(
    tmp_home: Path, fake_kc: FakeKeychain, claude_config_path: Path
) -> None:
    """AE1: only oauthAccount changes in ~/.claude.json."""
    old_oauth = _oauth("old@x.com", "org-old", "Old")
    new_oauth = _oauth("new@x.com", "org-new", "New")
    _write_claude_config(
        claude_config_path,
        old_oauth,
        extra={
            "projects": {"/foo": {"history": ["a", "b", "c"]}},
            "telemetry": {"sessions": 99},
        },
    )
    fake_kc.write(CLAUDE_CREDENTIALS_SERVICE, "tester", "old-blob")
    fake_kc.write(CCSWITCH_KEYCHAIN_SERVICE, "new-acc", '{"claudeAiOauth": {"accessToken": "x"}}')

    console = _record_console()
    sw, store = _make_switcher(tmp_home, console=console)
    store.add(Account("old-acc", "old@x.com", "Old", old_oauth))
    store.add(Account("new-acc", "new@x.com", "New", new_oauth))

    sw.use("new-acc")

    config_after = json.loads(claude_config_path.read_text())
    assert config_after["oauthAccount"] == new_oauth
    assert config_after["projects"] == {"/foo": {"history": ["a", "b", "c"]}}
    assert config_after["telemetry"] == {"sessions": 99}
    assert config_after["userID"] == "user-xyz"
    assert config_after["version"] == "1.0.0"
    # AE2: live Keychain entry now contains the saved blob.
    assert (
        fake_kc.entries[(CLAUDE_CREDENTIALS_SERVICE, "tester")]
        == '{"claudeAiOauth": {"accessToken": "x"}}'
    )
    out = console.export_text()
    assert "30" in out  # cache warning seconds
    assert "restart" in out.lower() or "session" in out.lower()


def test_use_prompts_to_save_unsaved_active_then_proceeds(
    tmp_home: Path, fake_kc: FakeKeychain, claude_config_path: Path
) -> None:
    """AE6: active account not saved → prompt to save first."""
    active_oauth = _oauth("active@x.com", "org-active", "ActiveOrg")
    target_oauth = _oauth("target@x.com", "org-target", "TargetOrg")
    _write_claude_config(claude_config_path, active_oauth)
    active_blob = '{"claudeAiOauth": {"accessToken": "a"}}'
    target_blob = '{"claudeAiOauth": {"accessToken": "t"}}'
    fake_kc.write(CLAUDE_CREDENTIALS_SERVICE, "tester", active_blob)
    fake_kc.write(CCSWITCH_KEYCHAIN_SERVICE, "target", target_blob)

    sw, store = _make_switcher(tmp_home)
    store.add(Account("target", "target@x.com", "TargetOrg", target_oauth))

    sw._confirm = lambda *_a, **_kw: True  # type: ignore[assignment]
    sw.use("target")

    # Active was saved with the suggested default name.
    assert store.get("active-activeorg") is not None
    assert fake_kc.entries[(CCSWITCH_KEYCHAIN_SERVICE, "active-activeorg")] == active_blob
    # Switch then proceeded.
    config_after = json.loads(claude_config_path.read_text())
    assert config_after["oauthAccount"] == target_oauth


def test_use_aborts_when_user_declines_save_prompt(
    tmp_home: Path, fake_kc: FakeKeychain, claude_config_path: Path
) -> None:
    active_oauth = _oauth("active@x.com", "org-active")
    target_oauth = _oauth("target@x.com", "org-target")
    _write_claude_config(claude_config_path, active_oauth)
    active_blob = '{"claudeAiOauth": {"accessToken": "a"}}'
    target_blob = '{"claudeAiOauth": {"accessToken": "t"}}'
    fake_kc.write(CLAUDE_CREDENTIALS_SERVICE, "tester", active_blob)
    fake_kc.write(CCSWITCH_KEYCHAIN_SERVICE, "target", target_blob)

    sw, store = _make_switcher(tmp_home)
    store.add(Account("target", "target@x.com", "Org", target_oauth))

    sw._confirm = lambda *_a, **_kw: False  # type: ignore[assignment]
    sw.use("target")

    # Nothing changed.
    config_after = json.loads(claude_config_path.read_text())
    assert config_after["oauthAccount"] == active_oauth
    assert fake_kc.entries[(CLAUDE_CREDENTIALS_SERVICE, "tester")] == active_blob


def test_use_missing_keychain_entry_aborts_before_any_write(
    tmp_home: Path, fake_kc: FakeKeychain, claude_config_path: Path
) -> None:
    """AE7: missing Keychain entry → abort before any write."""
    active_oauth = _oauth("work@example.com", "org-1")
    target_oauth = _oauth("personal@example.com", "org-2")
    _write_claude_config(claude_config_path, active_oauth)
    fake_kc.write(CLAUDE_CREDENTIALS_SERVICE, "tester", "active-blob")
    # Intentionally do NOT write the ccswitch:personal Keychain entry.

    sw, store = _make_switcher(tmp_home)
    store.add(Account("work", "work@example.com", "Example Org", active_oauth))
    store.add(Account("personal", "personal@example.com", "Personal", target_oauth))

    config_before = claude_config_path.read_text()
    with pytest.raises(ValueError, match="missing"):
        sw.use("personal")

    # Live state untouched.
    assert claude_config_path.read_text() == config_before
    assert fake_kc.entries[(CLAUDE_CREDENTIALS_SERVICE, "tester")] == "active-blob"


def test_use_unparseable_blob_aborts_before_any_write(
    tmp_home: Path, fake_kc: FakeKeychain, claude_config_path: Path
) -> None:
    active_oauth = _oauth("work@example.com", "org-1")
    target_oauth = _oauth("personal@example.com", "org-2")
    _write_claude_config(claude_config_path, active_oauth)
    fake_kc.write(CLAUDE_CREDENTIALS_SERVICE, "tester", "active-blob")
    fake_kc.write(CCSWITCH_KEYCHAIN_SERVICE, "personal", "{not json")

    sw, store = _make_switcher(tmp_home)
    store.add(Account("work", "work@example.com", "Example Org", active_oauth))
    store.add(Account("personal", "personal@example.com", "Personal", target_oauth))

    config_before = claude_config_path.read_text()
    with pytest.raises(ValueError, match="not valid JSON"):
        sw.use("personal")
    assert claude_config_path.read_text() == config_before
    assert fake_kc.entries[(CLAUDE_CREDENTIALS_SERVICE, "tester")] == "active-blob"


def test_use_missing_claude_config_raises_file_not_found(
    tmp_home: Path, fake_kc: FakeKeychain
) -> None:
    fake_kc.write(CCSWITCH_KEYCHAIN_SERVICE, "work", '{"claudeAiOauth": {}}')

    sw, store = _make_switcher(tmp_home)
    store.add(Account("work", "work@example.com", "Example Org", _oauth("work@example.com", "org-1")))

    with pytest.raises(FileNotFoundError):
        sw.use("work")


def test_use_unparseable_claude_config_raises_value_error(
    tmp_home: Path, fake_kc: FakeKeychain, claude_config_path: Path
) -> None:
    claude_config_path.write_text("{not json")
    fake_kc.write(CCSWITCH_KEYCHAIN_SERVICE, "work", '{"claudeAiOauth": {}}')

    sw, store = _make_switcher(tmp_home)
    store.add(Account("work", "work@example.com", "Example Org", _oauth("work@example.com", "org-1")))

    with pytest.raises(ValueError, match="Cannot parse"):
        sw.use("work")


def test_use_unknown_label_raises(
    tmp_home: Path, fake_kc: FakeKeychain, claude_config_path: Path
) -> None:
    _write_claude_config(claude_config_path, _oauth("work@example.com", "org-1"))
    sw, _ = _make_switcher(tmp_home)
    with pytest.raises(ValueError, match="No saved account"):
        sw.use("ghost")


# ---- remove -------------------------------------------------------------


def test_remove_happy_path(
    tmp_home: Path, fake_kc: FakeKeychain
) -> None:
    sw, store = _make_switcher(tmp_home)
    store.add(Account("work", "work@example.com", "Example Org", _oauth("work@example.com", "org-1")))
    fake_kc.write(CCSWITCH_KEYCHAIN_SERVICE, "work", "blob")

    sw._confirm = lambda *_a, **_kw: True  # type: ignore[assignment]
    sw.remove("work")

    assert store.get("work") is None
    assert (CCSWITCH_KEYCHAIN_SERVICE, "work") not in fake_kc.entries


def test_remove_active_label_warns_but_proceeds(
    tmp_home: Path, fake_kc: FakeKeychain, claude_config_path: Path
) -> None:
    """AE8: removing the active label warns; Claude state unchanged."""
    active_oauth = _oauth("work@example.com", "org-1", "Example Org")
    _write_claude_config(claude_config_path, active_oauth)
    fake_kc.write(CLAUDE_CREDENTIALS_SERVICE, "tester", "active-blob")
    fake_kc.write(CCSWITCH_KEYCHAIN_SERVICE, "work", "saved-blob")

    console = _record_console()
    sw, store = _make_switcher(tmp_home, console=console)
    store.add(Account("work", "work@example.com", "Example Org", active_oauth))

    sw._confirm = lambda *_a, **_kw: True  # type: ignore[assignment]
    sw.remove("work")

    out = console.export_text()
    assert "currently active" in out.lower() or "Warning" in out
    # Saved metadata + ccswitch keychain entry gone.
    assert store.get("work") is None
    assert (CCSWITCH_KEYCHAIN_SERVICE, "work") not in fake_kc.entries
    # Live Claude state untouched.
    assert fake_kc.entries[(CLAUDE_CREDENTIALS_SERVICE, "tester")] == "active-blob"
    config_after = json.loads(claude_config_path.read_text())
    assert config_after["oauthAccount"] == active_oauth


def test_remove_user_declines_no_changes(
    tmp_home: Path, fake_kc: FakeKeychain
) -> None:
    sw, store = _make_switcher(tmp_home)
    store.add(Account("work", "work@example.com", "Example Org", _oauth("work@example.com", "org-1")))
    fake_kc.write(CCSWITCH_KEYCHAIN_SERVICE, "work", "blob")

    sw._confirm = lambda *_a, **_kw: False  # type: ignore[assignment]
    sw.remove("work")

    assert store.get("work") is not None
    assert fake_kc.entries[(CCSWITCH_KEYCHAIN_SERVICE, "work")] == "blob"


def test_remove_unknown_label_raises(
    tmp_home: Path, fake_kc: FakeKeychain
) -> None:
    sw, _ = _make_switcher(tmp_home)
    with pytest.raises(ValueError, match="No saved account"):
        sw.remove("ghost")


# ---- refresh-on-switch --------------------------------------------------


def _expired_blob(refresh: str = "refresh-tok") -> str:
    return json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": "old-access",
                "refreshToken": refresh,
                "expiresAt": 1,  # ~epoch — definitely expired
                "scopes": ["user:inference"],
            }
        }
    )


def _fresh_blob() -> str:
    from datetime import datetime, timezone
    future_ms = int(datetime.now(timezone.utc).timestamp() * 1000) + 60 * 60 * 1000
    return json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": "fresh-access",
                "refreshToken": "refresh-tok",
                "expiresAt": future_ms,
                "scopes": ["user:inference"],
            }
        }
    )


def test_use_refreshes_expired_saved_blob_before_writing_live(
    tmp_home: Path,
    fake_kc: FakeKeychain,
    claude_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_oauth = _oauth("work@example.com", "org-1")
    target_oauth = _oauth("personal@example.com", "org-2")
    _write_claude_config(claude_config_path, active_oauth)
    fake_kc.write(CLAUDE_CREDENTIALS_SERVICE, "tester", _fresh_blob())
    fake_kc.write(CCSWITCH_KEYCHAIN_SERVICE, "work", _fresh_blob())  # active is saved
    fake_kc.write(CCSWITCH_KEYCHAIN_SERVICE, "personal", _expired_blob())

    refreshed_blob = json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": "MINT-NEW",
                "refreshToken": "refresh-tok",
                "expiresAt": 99999999999999,
                "scopes": ["user:inference"],
            }
        }
    )
    monkeypatch.setattr(
        switcher_mod, "refresh_oauth_credentials", lambda _b: refreshed_blob
    )

    sw, store = _make_switcher(tmp_home)
    store.add(Account("work", "work@example.com", "Example Org", active_oauth))
    store.add(Account("personal", "personal@example.com", "Personal", target_oauth))

    sw.use("personal")

    # Live Claude Code-credentials got the REFRESHED blob, not the original expired one.
    assert fake_kc.entries[(CLAUDE_CREDENTIALS_SERVICE, "tester")] == refreshed_blob
    # The saved ccswitch:personal entry was also updated so future switches don't re-refresh.
    assert fake_kc.entries[(CCSWITCH_KEYCHAIN_SERVICE, "personal")] == refreshed_blob


def test_use_skips_refresh_when_token_still_fresh(
    tmp_home: Path,
    fake_kc: FakeKeychain,
    claude_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_oauth = _oauth("work@example.com", "org-1")
    target_oauth = _oauth("personal@example.com", "org-2")
    _write_claude_config(claude_config_path, active_oauth)
    fake_kc.write(CLAUDE_CREDENTIALS_SERVICE, "tester", _fresh_blob())
    fake_kc.write(CCSWITCH_KEYCHAIN_SERVICE, "work", _fresh_blob())
    fresh_target = _fresh_blob()
    fake_kc.write(CCSWITCH_KEYCHAIN_SERVICE, "personal", fresh_target)

    refresh_called = False

    def boom(_blob: str) -> None:
        nonlocal refresh_called
        refresh_called = True
        return None

    monkeypatch.setattr(switcher_mod, "refresh_oauth_credentials", boom)

    sw, store = _make_switcher(tmp_home)
    store.add(Account("work", "work@example.com", "Example Org", active_oauth))
    store.add(Account("personal", "personal@example.com", "Personal", target_oauth))

    sw.use("personal")

    assert refresh_called is False
    assert fake_kc.entries[(CLAUDE_CREDENTIALS_SERVICE, "tester")] == fresh_target


def test_use_warns_and_proceeds_when_refresh_fails(
    tmp_home: Path,
    fake_kc: FakeKeychain,
    claude_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_oauth = _oauth("work@example.com", "org-1")
    target_oauth = _oauth("personal@example.com", "org-2")
    _write_claude_config(claude_config_path, active_oauth)
    fake_kc.write(CLAUDE_CREDENTIALS_SERVICE, "tester", _fresh_blob())
    fake_kc.write(CCSWITCH_KEYCHAIN_SERVICE, "work", _fresh_blob())
    expired = _expired_blob()
    fake_kc.write(CCSWITCH_KEYCHAIN_SERVICE, "personal", expired)

    monkeypatch.setattr(switcher_mod, "refresh_oauth_credentials", lambda _b: None)

    console = _record_console()
    sw, store = _make_switcher(tmp_home, console=console)
    store.add(Account("work", "work@example.com", "Example Org", active_oauth))
    store.add(Account("personal", "personal@example.com", "Personal", target_oauth))

    sw.use("personal")

    out = console.export_text()
    assert "expired" in out
    assert "re-login" in out.lower() or "ccswitch add" in out
    # Stale blob was still written through to live state (Claude Code will show its own 401).
    assert fake_kc.entries[(CLAUDE_CREDENTIALS_SERVICE, "tester")] == expired


# ---- error propagation --------------------------------------------------


def test_keychain_error_propagates(
    tmp_home: Path, fake_kc: FakeKeychain, claude_config_path: Path
) -> None:
    """A real KeychainError from the keychain layer must bubble up so the CLI can catch it."""
    _write_claude_config(claude_config_path, _oauth("work@example.com", "org-1"))

    def boom(*_args: object, **_kwargs: object) -> None:
        raise KeychainError("kc broken")

    import ccswitch.switcher as sw_mod
    original_read = sw_mod.keychain_read
    sw_mod.keychain_read = boom  # type: ignore[assignment]
    try:
        sw, _ = _make_switcher(tmp_home)
        with pytest.raises(KeychainError, match="kc broken"):
            sw.add(label_override="x")
    finally:
        sw_mod.keychain_read = original_read  # type: ignore[assignment]
