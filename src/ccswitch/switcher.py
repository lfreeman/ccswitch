"""Orchestration for ``add``, ``list``, ``current``, ``use``, and ``remove``.

The :class:`Switcher` composes the lower-level modules (paths, jsonio,
keychain, accounts) and owns the read-before-write discipline that makes
the ``use`` operation safe: every read happens before any write, so an
unreadable credential blob or malformed ``~/.claude.json`` aborts the
switch with both the live config and the live Keychain entry untouched.
"""

from __future__ import annotations

import json
import os
from typing import Any

from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

from ccswitch.accounts import (
    Account,
    AccountStore,
    suggest_label,
    validate_label,
)
from ccswitch.jsonio import read_json, write_json_atomic
from ccswitch.keychain import read as keychain_read
from ccswitch.keychain import write as keychain_write
from ccswitch.keychain import delete as keychain_delete
from ccswitch.oauth import (
    extract_oauth_data,
    is_oauth_token_expired,
    refresh_oauth_credentials,
)
from ccswitch.paths import get_claude_config_path

CLAUDE_CREDENTIALS_SERVICE = "Claude Code-credentials"
CCSWITCH_KEYCHAIN_SERVICE = "ccswitch"
SETUP_TOKEN_SERVICE = "ccswitch-token"
KEYCHAIN_CACHE_WARNING_SECONDS = 30


def _current_user() -> str:
    return os.environ.get("USER", "user")


def _active_identity(claude_config: dict[str, Any] | None) -> tuple[str, str] | None:
    """Return ``(emailAddress, organizationUuid)`` of the active account, or ``None``."""
    if not isinstance(claude_config, dict):
        return None
    oauth = claude_config.get("oauthAccount")
    if not isinstance(oauth, dict):
        return None
    email = oauth.get("emailAddress")
    org_uuid = oauth.get("organizationUuid")
    if not isinstance(email, str) or not isinstance(org_uuid, str):
        return None
    return (email, org_uuid)


def _account_identity(account: Account) -> tuple[str, str] | None:
    oauth = account.oauth_account
    email = oauth.get("emailAddress")
    org_uuid = oauth.get("organizationUuid")
    if not isinstance(email, str) or not isinstance(org_uuid, str):
        return None
    return (email, org_uuid)


class Switcher:
    """Stateful façade over the on-disk store + Keychain + live Claude config."""

    def __init__(
        self,
        store: AccountStore | None = None,
        console: Console | None = None,
    ) -> None:
        self._store = store if store is not None else AccountStore()
        self._console = console if console is not None else Console()
        self._user = _current_user()

    # ---- Prompts (extracted so tests can monkey-patch on the instance) ----

    def _prompt_text(
        self, message: str, default: str | None = None, *, password: bool = False
    ) -> str:
        return Prompt.ask(
            message, default=default, password=password, console=self._console
        )

    def _confirm(self, message: str, *, default: bool = False) -> bool:
        return Confirm.ask(message, default=default, console=self._console)

    # ---- Internal helpers --------------------------------------------------

    def _read_claude_config(self) -> dict[str, Any]:
        """Return ``~/.claude.json`` parsed, raising on missing / unparseable.

        Required for R20: ``use`` aborts cleanly when the live config is gone
        or corrupt rather than rebuilding it from scratch.
        """
        path = get_claude_config_path()
        try:
            data = read_json(path)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Cannot parse Claude config at {path}: {exc.msg}. "
                f"Refusing to overwrite — fix or remove the file first."
            ) from exc
        if data is None:
            raise FileNotFoundError(
                f"Claude config not found at {path}. Run Claude Code at least "
                f"once to sign in before using ccswitch."
            )
        return data

    def _read_active_credentials_blob(self) -> str:
        blob = keychain_read(CLAUDE_CREDENTIALS_SERVICE, self._user)
        if not blob:
            raise ValueError(
                f"No active Claude Code credentials found in Keychain "
                f"(service={CLAUDE_CREDENTIALS_SERVICE!r}, account={self._user!r}). "
                f"Sign in via Claude Code first."
            )
        return blob

    def _read_saved_blob(self, label: str) -> str:
        blob = keychain_read(CCSWITCH_KEYCHAIN_SERVICE, label)
        if not blob:
            raise ValueError(
                f"Saved Keychain entry for label {label!r} is missing. "
                f"Re-run `ccswitch add` to repair it."
            )
        try:
            json.loads(blob)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Saved Keychain entry for label {label!r} is not valid JSON: {exc.msg}."
            ) from exc
        return blob

    def _refresh_if_expired(self, label: str, blob: str) -> str:
        """Refresh the access token in ``blob`` when expired; persist back to Keychain.

        Best-effort: on refresh failure, returns the original blob with a
        warning. Claude Code will surface its own 401 in that case.
        """
        oauth = extract_oauth_data(blob)
        if not oauth or not is_oauth_token_expired(oauth.get("expiresAt")):
            return blob
        refreshed = refresh_oauth_credentials(blob)
        if refreshed is None:
            self._console.print(
                "[yellow]Warning:[/yellow] saved access token is expired and refresh "
                f"failed (refresh token may have been revoked by a Claude Code /logout). "
                f"If `claude` reports 401 errors, re-login to the '{label}' account in "
                f"Claude Code and run `ccswitch add --label {label}` to repair."
            )
            return blob
        # Persist refreshed credentials so the next switch/exec doesn't re-refresh.
        keychain_write(CCSWITCH_KEYCHAIN_SERVICE, label, refreshed)
        return refreshed

    def _print_active_marker_table(self, active: tuple[str, str] | None) -> None:
        table = Table(title="ccswitch accounts")
        table.add_column("Label", style="bold")
        table.add_column("Email")
        table.add_column("Organization")
        table.add_column("Active", justify="center")
        for acc in self._store.list():
            marker = ""
            if active is not None and _account_identity(acc) == active:
                marker = "[green]●[/green]"
            table.add_row(acc.label, acc.email, acc.organization_name, marker)
        self._console.print(table)

    # ---- Public commands ---------------------------------------------------

    def add(self, label_override: str | None = None) -> None:
        """Save the currently-active Claude Code account."""
        config = self._read_claude_config()
        oauth = config.get("oauthAccount")
        if not isinstance(oauth, dict):
            raise ValueError(
                "Active Claude Code config has no oauthAccount section. "
                "Sign in via Claude Code first."
            )
        blob = self._read_active_credentials_blob()

        email = str(oauth.get("emailAddress", ""))
        org_name = str(oauth.get("organizationName", ""))
        suggested = suggest_label(email, org_name)

        if label_override is None:
            label = self._prompt_text("Label for this account", default=suggested or None)
        else:
            label = label_override
        validate_label(label)

        existing = self._store.get(label)
        if existing is not None:
            same_identity = _account_identity(existing) == _active_identity(config)
            if same_identity:
                if not self._confirm(
                    f"Label '{label}' already saved for this account. Update credentials in place?",
                    default=True,
                ):
                    self._console.print("[yellow]Cancelled.[/yellow]")
                    return
            else:
                raise ValueError(
                    f"Label {label!r} is already in use by a different account. "
                    f"Pick a different label or remove the existing one first."
                )

        account = Account(
            label=label,
            email=email,
            organization_name=org_name,
            oauth_account=oauth,
        )
        keychain_write(CCSWITCH_KEYCHAIN_SERVICE, label, blob)
        self._store.add(account)
        self._console.print(f"[green]Saved[/green] account '{label}' ({email}).")

    def add_token(self, label: str, token: str | None = None) -> None:
        """Attach a long-lived ``claude setup-token`` to an already-saved account.

        The setup-token is what ``exec`` injects as ``CLAUDE_CODE_OAUTH_TOKEN``;
        unlike the browser-login blob it does not share the interactive login's
        rotating refresh chain, so it keeps working when the same account is
        also live in Claude Code. ``token`` is prompted for (hidden) when not
        supplied. Raises ``ValueError`` if ``label`` is not a saved account or
        the token is empty.
        """
        if self._store.get(label) is None:
            raise ValueError(
                f"No saved account labeled {label!r}. Run `ccswitch add` first to "
                f"save the account, then attach a token with `ccswitch add-token {label}`."
            )
        if token is None:
            token = self._prompt_text(
                f"Paste a `claude setup-token` for '{label}'", password=True
            )
        token = token.strip()
        if not token:
            raise ValueError("No token provided.")
        keychain_write(SETUP_TOKEN_SERVICE, label, token)
        self._console.print(
            f"[green]Saved[/green] exec token for '{label}'. "
            f"`ccswitch exec {label} -- claude …` will now use it."
        )

    def list_accounts(self) -> None:
        """Render saved accounts with a marker on the currently-active one."""
        if len(self._store) == 0:
            self._console.print(
                "[yellow]No saved accounts.[/yellow] Run `ccswitch add` to save the "
                "currently-active Claude Code account."
            )
            return
        try:
            config = self._read_claude_config()
        except (FileNotFoundError, ValueError):
            config = None
        active = _active_identity(config) if config is not None else None
        self._print_active_marker_table(active)

    def current(self) -> None:
        """Print the currently-active Claude account."""
        try:
            config = self._read_claude_config()
        except (FileNotFoundError, ValueError) as exc:
            self._console.print(f"[red]{exc}[/red]")
            return
        oauth = config.get("oauthAccount")
        if not isinstance(oauth, dict):
            self._console.print("[yellow]No active Claude account.[/yellow]")
            return
        email = str(oauth.get("emailAddress", "(unknown email)"))
        org_name = str(oauth.get("organizationName", "(no organization)"))
        identity = _active_identity(config)
        matched_label: str | None = None
        if identity is not None:
            for acc in self._store.list():
                if _account_identity(acc) == identity:
                    matched_label = acc.label
                    break
        if matched_label is None:
            self._console.print(
                f"Active: [bold]{email}[/bold] · {org_name} "
                f"[dim](not saved — run `ccswitch add` to save it)[/dim]"
            )
        else:
            self._console.print(
                f"Active: [bold green]{matched_label}[/bold green] "
                f"({email} · {org_name})"
            )

    def use(self, label: str) -> None:
        """Switch the global Claude Code config + Keychain entry to ``label``."""
        target = self._store.get(label)
        if target is None:
            raise ValueError(
                f"No saved account labeled {label!r}. Run `ccswitch list` to see saved labels."
            )

        # ---- READ phase (everything before any mutation) ----
        saved_blob = self._read_saved_blob(label)
        config = self._read_claude_config()
        saved_blob = self._refresh_if_expired(label, saved_blob)
        # The live Keychain entry holds the currently-active account's freshest
        # (possibly just-rotated) credentials. Read it now so we can preserve it
        # before the write phase overwrites it. Tolerate absence — older configs
        # or a missing entry simply mean there is nothing to preserve.
        live_blob = keychain_read(CLAUDE_CREDENTIALS_SERVICE, self._user)

        # Identify the currently-active account against the saved set.
        active_identity = _active_identity(config)
        active_label = None
        if active_identity is not None:
            active_label = next(
                (
                    acc.label
                    for acc in self._store.list()
                    if _account_identity(acc) == active_identity
                ),
                None,
            )

        if active_identity is not None and active_label is None:
            # R18: the active account is not saved — offer to save it first so
            # its credentials aren't lost when we overwrite the live entry.
            oauth = config.get("oauthAccount", {})
            email = str(oauth.get("emailAddress", ""))
            org_name = str(oauth.get("organizationName", ""))
            suggested = suggest_label(email, org_name)
            if self._confirm(
                f"The currently-active account ({email}) is not saved. "
                f"Save it as '{suggested}' before switching?",
                default=True,
            ):
                self.add(label_override=suggested)
            else:
                self._console.print(
                    "[yellow]Aborted.[/yellow] Use `ccswitch add --label <name>` "
                    "to save it manually."
                )
                return

        # ---- WRITE phase ----
        # Re-capture the active account's live (rotated) credentials into its
        # saved entry before we overwrite the live entry. Without this, its saved
        # copy keeps a refresh token the live app already rotated out, so a later
        # `use` of that account fails to refresh. Skipped when switching to the
        # already-active account (it would only clobber its own saved copy).
        if active_label is not None and active_label != label and live_blob:
            keychain_write(CCSWITCH_KEYCHAIN_SERVICE, active_label, live_blob)

        new_config = dict(config)
        new_config["oauthAccount"] = target.oauth_account
        write_json_atomic(get_claude_config_path(), new_config)
        keychain_write(CLAUDE_CREDENTIALS_SERVICE, self._user, saved_blob)

        self._console.print(
            f"[green]Switched[/green] to '{label}' ({target.email})."
        )
        self._console.print(
            f"[dim]Note: macOS Keychain caches credentials for ~{KEYCHAIN_CACHE_WARNING_SECONDS} "
            f"seconds. A running Claude Code session will not reflect the change until "
            f"restart or cache expiry.[/dim]"
        )

    def remove(self, label: str) -> None:
        """Delete a saved account after interactive confirmation."""
        target = self._store.get(label)
        if target is None:
            raise ValueError(f"No saved account labeled {label!r}.")

        try:
            config = self._read_claude_config()
        except (FileNotFoundError, ValueError):
            config = None
        is_active = (
            config is not None
            and _account_identity(target) == _active_identity(config)
        )
        if is_active:
            self._console.print(
                f"[yellow]Warning:[/yellow] '{label}' is currently active in "
                f"`~/.claude.json`. Removing it here does not log Claude Code out — "
                f"it only forgets ccswitch's saved copy."
            )

        if not self._confirm(
            f"Permanently remove saved account '{label}'?", default=False
        ):
            self._console.print("[yellow]Cancelled.[/yellow]")
            return

        keychain_delete(CCSWITCH_KEYCHAIN_SERVICE, label)
        keychain_delete(SETUP_TOKEN_SERVICE, label)
        self._store.remove(label)
        self._console.print(f"[green]Removed[/green] '{label}'.")
