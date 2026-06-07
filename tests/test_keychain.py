"""Tests for ccswitch.keychain."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from ccswitch import keychain
from ccswitch.keychain import (
    ITEM_NOT_FOUND_EXIT_CODE,
    SECURITY_CLI,
    KeychainError,
    delete,
    read,
    write,
)


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _stub_run(
    monkeypatch: pytest.MonkeyPatch, completed: _FakeCompleted
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_run(args: list[str], **kwargs: Any) -> _FakeCompleted:
        calls.append({"args": args, "kwargs": kwargs})
        return completed

    monkeypatch.setattr(keychain.subprocess, "run", fake_run)
    return calls


# ---- read --------------------------------------------------------------


def test_read_returns_stored_value(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_run(monkeypatch, _FakeCompleted(0, stdout="secret-blob\n"))
    assert read("ccswitch", "work") == "secret-blob"


def test_read_strips_only_trailing_newline(monkeypatch: pytest.MonkeyPatch) -> None:
    # security appends a single newline; embedded newlines in the blob must survive.
    _stub_run(monkeypatch, _FakeCompleted(0, stdout='{"a":1}\n{"b":2}\n'))
    assert read("ccswitch", "work") == '{"a":1}\n{"b":2}'


def test_read_returns_none_on_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_run(monkeypatch, _FakeCompleted(ITEM_NOT_FOUND_EXIT_CODE, stderr="not found"))
    assert read("ccswitch", "missing") is None


def test_read_raises_with_stderr_on_other_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_run(monkeypatch, _FakeCompleted(1, stderr="boom"))
    with pytest.raises(KeychainError, match="boom"):
        read("ccswitch", "work")


def test_read_passes_explicit_argv_no_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_run(monkeypatch, _FakeCompleted(0, stdout=""))
    read("ccswitch", "work")
    assert calls[0]["args"] == [
        SECURITY_CLI,
        "find-generic-password",
        "-a",
        "work",
        "-s",
        "ccswitch",
        "-w",
    ]
    assert "shell" not in calls[0]["kwargs"] or calls[0]["kwargs"].get("shell") is False


# ---- write -------------------------------------------------------------


def test_write_invokes_add_generic_password_with_update_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_run(monkeypatch, _FakeCompleted(0))
    write("ccswitch", "work", "blob")
    assert calls[0]["args"] == [
        SECURITY_CLI,
        "add-generic-password",
        "-U",
        "-s",
        "ccswitch",
        "-a",
        "work",
        "-w",
        "blob",
    ]


def test_write_raises_on_non_zero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_run(monkeypatch, _FakeCompleted(1, stderr="kaboom"))
    with pytest.raises(KeychainError, match="kaboom"):
        write("ccswitch", "work", "blob")


# ---- delete ------------------------------------------------------------


def test_delete_invokes_delete_generic_password(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_run(monkeypatch, _FakeCompleted(0))
    delete("ccswitch", "work")
    assert calls[0]["args"] == [
        SECURITY_CLI,
        "delete-generic-password",
        "-a",
        "work",
        "-s",
        "ccswitch",
    ]


def test_delete_succeeds_silently_on_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_run(monkeypatch, _FakeCompleted(ITEM_NOT_FOUND_EXIT_CODE, stderr="not found"))
    delete("ccswitch", "missing")  # no exception


def test_delete_raises_on_other_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_run(monkeypatch, _FakeCompleted(1, stderr="permission denied"))
    with pytest.raises(KeychainError, match="permission denied"):
        delete("ccswitch", "work")


# ---- safety ------------------------------------------------------------


def test_all_callers_pass_arg_lists_no_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    """All three callers must invoke subprocess.run with a list and no shell=True."""
    captured: list[tuple[Any, dict[str, Any]]] = []

    def fake_run(args: Any, **kwargs: Any) -> _FakeCompleted:
        captured.append((args, kwargs))
        return _FakeCompleted(0, stdout="")

    monkeypatch.setattr(keychain.subprocess, "run", fake_run)

    read("svc", "acct")
    write("svc", "acct", "value; rm -rf /")  # untrusted value
    delete("svc", "acct")

    for args, kwargs in captured:
        assert isinstance(args, list)
        assert all(isinstance(a, str) for a in args)
        assert kwargs.get("shell", False) is False


def test_subprocess_actually_called_through_module(monkeypatch: pytest.MonkeyPatch) -> None:
    # Sanity: the module references the real subprocess module so monkeypatching it works.
    assert keychain.subprocess is subprocess
