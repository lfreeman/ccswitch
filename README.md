# ccswitch

A small macOS-only CLI for managing multiple Claude Code accounts. Switch the
globally-active account, or scope a single command to a chosen account via
environment variables, without going through Claude Code's browser login again.

ccswitch is intentionally narrow: macOS only, one user, single machine. It
does not do Linux/Windows support, transactional rollback, usage stats, or
provide a TUI — by design.

## Install

```bash
uv tool install ccswitch
```

Requires Python 3.12+ and macOS.

## What it does

`ccswitch` stores each saved account as:

- a Keychain entry under service `ccswitch`, account `<label>` — the full
  credential blob (access token, refresh token, scopes)
- a row in `~/.config/ccswitch/accounts.json` — non-secret metadata (email,
  organization, the `oauthAccount` subtree from `~/.claude.json`)

Switching globally rewrites only the `oauthAccount` field inside
`~/.claude.json` and overwrites the macOS Keychain entry under service
`Claude Code-credentials`. Every other field in `~/.claude.json` — including
the project history that grows over time — is preserved byte-for-byte.

## Commands

### `ccswitch add [--label NAME]`

Save the currently-active Claude Code account. If `--label` is omitted, you'll
be prompted for one with a default of `<email-local>-<org-slug>` (e.g.,
`user-example-org`).

```bash
ccswitch add                    # prompts for a label
ccswitch add --label personal   # uses the given label directly
```

### `ccswitch list`

Show all saved accounts with a marker on whichever one matches the currently-
active `oauthAccount`.

### `ccswitch current`

Print the currently-active Claude account. If the active account isn't saved
yet, the output names the email and suggests `ccswitch add`.

### `ccswitch use <label>`

Switch the global Claude Code account to the saved `<label>`. Only the
`oauthAccount` subtree of `~/.claude.json` is rewritten; the Keychain entry
under service `Claude Code-credentials` is replaced with the saved blob.

```bash
ccswitch use work
```

If the currently-active account isn't yet saved, `use` will prompt you to save
it first so you don't lose access to its credentials.

> A running Claude Code session won't notice the switch right away: macOS
> Keychain caches reads for about 30 seconds. Restart the Claude Code app (or
> wait ~30s) for the change to take effect.

### `ccswitch exec <label> -- <cmd...>`

Run `<cmd>` once with environment variables scoped to `<label>` — `~/.claude.json`
and the global Keychain entry are not modified. Useful for pipelines that
invoke `claude` or `claude_agent_sdk` and need a specific account without
disturbing the interactive session:

```bash
ccswitch exec work -- claude -p "say hello"
ccswitch exec personal -- claude -p "say hello"
```

The child process receives:

- `CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_CODE_OAUTH_REFRESH_TOKEN`,
  `CLAUDE_CODE_OAUTH_SCOPES` — extracted from the saved credential blob
- `CLAUDE_CONFIG_DIR` — a per-invocation temp directory with a minimal
  `.claude.json` containing the target account's `oauthAccount`

The temp directory is removed when the child exits.

### `ccswitch remove <label>`

Delete a saved account after a `[y/N]` confirmation. The matching Keychain
entry is removed; the row in `accounts.json` goes with it. Live Claude Code
state (whatever's in `~/.claude.json` and the `Claude Code-credentials`
Keychain entry) is left alone.

## Where things live

- `~/.claude.json` — Claude Code's own config (ccswitch only ever touches the
  `oauthAccount` subtree)
- `~/.config/ccswitch/accounts.json` — ccswitch's metadata, mode `0600`
- macOS Keychain service `ccswitch`, account `<label>` — per-account credential
  blobs
- macOS Keychain service `Claude Code-credentials`, account `$USER` —
  Claude Code's live credentials (only `use` writes here)

`CLAUDE_CONFIG_DIR` is honored when set, and the legacy `<config_home>/.config.json`
location takes precedence over `~/.claude.json` if it exists — same resolution
order Claude Code itself uses.

## Token refresh

Claude Code access tokens expire on the order of an hour. `ccswitch use` and
`ccswitch exec` automatically refresh an expired saved token against the
OAuth endpoint before handing it back to Claude Code — the refreshed token
is also persisted back into the per-label Keychain entry so the next switch
doesn't need to refresh again.

If the refresh fails — typically because Claude Code's `/logout` command was
run while that account was active, which revokes the refresh token
server-side — the switch falls through with a warning. Recover by:

1. Logging Claude Code back into the affected account via `/login` (avoid
   `/logout` first if you can).
2. Re-running `ccswitch add --label <name>` to capture fresh credentials.

> **Avoid `/logout` once you have saved accounts.** Use `ccswitch use <label>`
> to switch — it only rewrites the Keychain entry locally; the previous
> account's refresh token stays valid.

## Troubleshooting

- **A `use` doesn't appear to take effect right away.** The macOS Keychain
  caches reads for ~30s. Restart the Claude Code app or wait for the cache to
  expire.
- **`ccswitch use` reports a missing Keychain entry.** The per-label entry
  (service `ccswitch`, account `<label>`) was removed externally. Run
  `ccswitch add` again with the same label to repair it.
- **`Please run /login · API Error: 401` after a switch.** The saved
  refresh token has been revoked (usually by a prior `/logout`). See
  [Token refresh](#token-refresh) for the recovery.
- **`exec` with `claude` returns a 403 on profile endpoints.** Claude Code
  expects narrow `user:inference` scope for env-var-injected tokens; broad
  browser-login tokens may not work for every operation. Report it as an
  issue — the fallback is to register a narrow `claude setup-token` and pass
  that through.
