# ccswitch

A small macOS-only CLI for managing multiple Claude Code accounts. Switch the
globally-active account, or scope a single command to a chosen account via
environment variables, without going through Claude Code's browser login again.

The scope is deliberately narrow: macOS only, one user, one machine. There is
no Linux or Windows support, no transactional rollback, no usage statistics,
and no TUI.

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

### `ccswitch add-token <label>`

Attach a long-lived `claude setup-token` to an account you have already saved,
for use by `ccswitch exec`. The browser-login credentials captured by `add`
share a refresh token that Claude Code rotates about once an hour, so a saved
copy goes stale quickly (see [Token refresh](#token-refresh)). A setup-token
lasts roughly a year and is not part of that rotating chain.

```bash
# 1. Sign in to the target account, then create a token (it prints to your terminal):
claude setup-token
# 2. Paste it into ccswitch (prompted for, with the input hidden, if --token is omitted):
ccswitch add-token work
```

When a setup-token is present, `exec` uses it and leaves the rotating refresh
chain alone.

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

Run `<cmd>` once with environment variables scoped to `<label>`. Neither
`~/.claude.json` nor the global Keychain entry is modified. This is useful for
pipelines that invoke `claude` or `claude_agent_sdk` and need a specific
account without disturbing the interactive session:

```bash
ccswitch exec work -- claude -p "say hello"
ccswitch exec personal -- claude -p "say hello"
```

Credentials come from one of two sources, in order:

1. **A `claude setup-token` saved with `ccswitch add-token`.** It is injected
   as `CLAUDE_CODE_OAUTH_TOKEN`. This is the reliable source for `exec`,
   because the token is not part of the interactive login's rotating refresh
   chain and keeps working while the same account is signed in to Claude Code.
2. **The saved browser-login credentials**, used only when no setup-token
   exists. They are injected as `CLAUDE_CODE_OAUTH_TOKEN`,
   `CLAUDE_CODE_OAUTH_REFRESH_TOKEN`, and `CLAUDE_CODE_OAUTH_SCOPES`, and an
   expired access token is refreshed first. If the access token has expired and
   its refresh token has already been rotated out, `exec` stops with an error
   that points to `add-token` rather than starting a command that would fail
   with a 401.

In both cases the child process also receives `CLAUDE_CONFIG_DIR`, a
per-invocation temporary directory holding a minimal `.claude.json` with the
target account's `oauthAccount`. That directory is deleted when the child exits.

> **One caveat, and only for the browser-login source.** On that path the child
> receives a refresh token. If a long-running `exec` outlives the access token
> (about an hour), Claude Code refreshes the token itself. The refresh rotates
> the refresh token server-side, but the new values are written into the
> temporary `CLAUDE_CONFIG_DIR` that `exec` then deletes. ccswitch never sees
> them, so both the saved copy and the live copy are left holding a refresh
> token the server has already invalidated. `ccswitch add-token` avoids the
> problem: a setup-token carries no refresh token and is never rotated.

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

Claude Code access tokens expire after about an hour. `ccswitch use` and
`ccswitch exec` refresh an expired saved token against the OAuth endpoint
before handing it back to Claude Code. The refreshed token is also written
back to the per-label Keychain entry, so the next switch does not have to
refresh again.

Refresh tokens rotate. Each time an account refreshes, which most often happens
inside the running Claude Code app, the server issues a new refresh token and
invalidates the previous one. Saved browser-login credentials for an account
you also use interactively can therefore have their stored refresh token
invalidated within the hour, after which refresh fails. For `exec`, the durable
fix is `ccswitch add-token`, which stores a setup-token outside the rotating
chain. `ccswitch use` is not affected, because it writes the saved credentials
into the live Keychain entry and leaves Claude Code as the sole owner of the
chain.

When refresh fails, `ccswitch use` continues with a warning and `ccswitch exec`
stops with a recovery hint. To recover:

1. Sign back in to the affected account with `/login` in Claude Code, avoiding
   `/logout` first if you can.
2. Run `ccswitch add --label <name>` again to capture fresh credentials.

> **Avoid `/logout` once you have saved accounts.** Switch with
> `ccswitch use <label>` instead. It only rewrites the Keychain entry locally,
> so the previous account's refresh token stays valid.

## Troubleshooting

- **A `use` does not appear to take effect right away.** The macOS Keychain
  caches reads for about 30 seconds. Restart the Claude Code app or wait for
  the cache to expire.
- **`ccswitch use` reports a missing Keychain entry.** The per-label entry
  (service `ccswitch`, account `<label>`) was removed outside of ccswitch. Run
  `ccswitch add` again with the same label to repair it.
- **`Please run /login · API Error: 401` after a switch.** The saved refresh
  token has been revoked, usually by an earlier `/logout`. See
  [Token refresh](#token-refresh) for the recovery steps.
- **`exec` reports expired credentials, or returns a 401.** The saved
  browser-login refresh token was rotated out by the running Claude Code app
  (see [Token refresh](#token-refresh)). Run `ccswitch add-token <label>` to
  attach a `claude setup-token`, which is the reliable credential source for
  `exec`.
