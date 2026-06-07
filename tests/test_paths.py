"""Tests for ccswitch.paths."""

from __future__ import annotations

from pathlib import Path

import pytest

from ccswitch.paths import (
    get_accounts_file,
    get_ccswitch_config_dir,
    get_claude_config_path,
)


def test_claude_config_path_defaults_to_home_dot_claude_json(tmp_home: Path) -> None:
    assert get_claude_config_path() == tmp_home / ".claude.json"


def test_claude_config_path_respects_claude_config_dir(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_home / "alt-config"
    override.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(override))
    assert get_claude_config_path() == override / ".claude.json"


def test_claude_config_path_returns_legacy_dot_config_json_when_present(
    tmp_home: Path,
) -> None:
    legacy_dir = tmp_home / ".claude"
    legacy_dir.mkdir()
    legacy = legacy_dir / ".config.json"
    legacy.write_text("{}")
    assert get_claude_config_path() == legacy


def test_claude_config_path_legacy_under_claude_config_dir(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_home / "alt-config"
    override.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(override))
    legacy = override / ".config.json"
    legacy.write_text("{}")
    assert get_claude_config_path() == legacy


def test_ccswitch_config_dir_defaults_to_home_dot_config_ccswitch(tmp_home: Path) -> None:
    assert get_ccswitch_config_dir() == tmp_home / ".config" / "ccswitch"


def test_ccswitch_config_dir_honors_absolute_xdg_config_home(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg = tmp_home / "xdg"
    xdg.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    assert get_ccswitch_config_dir() == xdg / "ccswitch"


def test_ccswitch_config_dir_ignores_relative_xdg_config_home(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/path")
    assert get_ccswitch_config_dir() == tmp_home / ".config" / "ccswitch"


def test_ccswitch_config_dir_ignores_empty_xdg_config_home(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    assert get_ccswitch_config_dir() == tmp_home / ".config" / "ccswitch"


def test_accounts_file_lives_inside_ccswitch_config_dir(tmp_home: Path) -> None:
    assert get_accounts_file() == tmp_home / ".config" / "ccswitch" / "accounts.json"
