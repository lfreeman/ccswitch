"""Wrapper around ``/usr/bin/security`` for macOS Keychain generic-password I/O.

ccswitch never reaches the Keychain except through this module. Each call
goes through ``subprocess.run`` with an explicit argument list — no
``shell=True``, no string concatenation — so untrusted labels can't smuggle
in shell metacharacters even if they slip past ``validate_label``.

Exit code 44 from ``security`` means "item not found" and is treated as a
``None`` return from :func:`read` and a no-op from :func:`delete`. Any
other non-zero exit becomes a :class:`KeychainError` carrying the captured
stderr so the caller has something useful to surface.
"""

from __future__ import annotations

import subprocess

SECURITY_CLI = "/usr/bin/security"
ITEM_NOT_FOUND_EXIT_CODE = 44


class KeychainError(RuntimeError):
    """Raised when ``/usr/bin/security`` exits non-zero for an unexpected reason."""


def read(service: str, account: str) -> str | None:
    """Return the stored password for ``(service, account)``, or ``None`` if absent."""
    result = subprocess.run(
        [SECURITY_CLI, "find-generic-password", "-a", account, "-s", service, "-w"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout.rstrip("\n")
    if result.returncode == ITEM_NOT_FOUND_EXIT_CODE:
        return None
    raise KeychainError(
        f"security find-generic-password failed (exit {result.returncode}): "
        f"{result.stderr.strip()}"
    )


def write(service: str, account: str, value: str) -> None:
    """Create or update the Keychain entry for ``(service, account)``."""
    result = subprocess.run(
        [SECURITY_CLI, "add-generic-password", "-U", "-s", service, "-a", account, "-w", value],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise KeychainError(
            f"security add-generic-password failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )


def delete(service: str, account: str) -> None:
    """Delete the Keychain entry for ``(service, account)``. No-op if absent."""
    result = subprocess.run(
        [SECURITY_CLI, "delete-generic-password", "-a", account, "-s", service],
        capture_output=True,
        text=True,
    )
    if result.returncode in (0, ITEM_NOT_FOUND_EXIT_CODE):
        return
    raise KeychainError(
        f"security delete-generic-password failed (exit {result.returncode}): "
        f"{result.stderr.strip()}"
    )
