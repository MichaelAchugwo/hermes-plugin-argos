# ARGOS

<p align="center">
  <img src="assets/argos-icon.png" width="180" alt="ARGOS guardian-eye icon">
</p>

![ARGOS — the many-eyed subscription guardian](assets/argos-hero.png)

**Autonomous Rotation & Governance of Subscriptions** — an open-source Hermes Agent plugin for pooled ChatGPT/Codex OAuth subscriptions.

Argos Panoptes was the hundred-eyed guardian who never stopped watching. ARGOS gives Hermes that same watchfulness across every Codex subscription: one pool, one quota view, persistent health state, automatic failover, and a manual switch when you want direct control. The name also keeps the mythology inside Hermes: the messenger god now orchestrates the all-seeing guardian.

## Features

- One card per `openai-codex` OAuth account in Hermes Desktop and the web dashboard.
- Live 5-hour and weekly remaining percentages, reset times, plan, health, and banked reset credits.
- Automatic quota-aware ordering with `least_weekly_remaining`, `fill_first`, or `round_robin` policy.
- Native Hermes same-turn recovery for plan caps, repeated 429s, and failed 401 refreshes.
- `hswitch`-style manual selection by index, id, label, or token fingerprint prefix.
- Serial OAuth keepalive using Hermes's existing Codex refresh helper.
- Cross-process locking, atomic auth writes, and a rolling set of 20 backups.
- No browser scraping, raw token output, or full email display.

## Requirements

- Hermes Agent with general Python plugins.
- Hermes Desktop plugin SDK for the native pane/page/chip (confirmed on Hermes v0.21.0).
- Optional web dashboard plugin system for the browser tab (confirmed on Hermes v0.21.0).
- Python 3.10+.

Older Hermes builds without the native Desktop SDK can still use the CLI and web-dashboard surface.

## Install

```bash
hermes plugins install MichaelAchugwo/hermes-plugin-argos --enable --force
```

Restart the gateway/Desktop backend so Python CLI/API registration reloads. In Desktop, open **Settings → Plugins**, enable **ARGOS** if needed, then run **Reload desktop plugins** from the command palette.

### Windows 10/11

Run in Git Bash, the Hermes terminal, or another shell where `hermes` is on `PATH`:

```bash
hermes plugins install MichaelAchugwo/hermes-plugin-argos --enable --force
hermes gateway restart
hermes subs doctor
```

Restart Hermes Desktop completely if its Python backend was already running. Then use **Settings → Plugins → ARGOS** and **Command Palette → Reload desktop plugins**. The default home is `%LOCALAPPDATA%\hermes`; this install uses the same active `$HERMES_HOME` as the CLI.

### macOS

Run in Terminal:

```bash
hermes plugins install MichaelAchugwo/hermes-plugin-argos --enable --force
hermes gateway restart
hermes subs doctor
```

Quit Hermes Desktop with **⌘Q** and reopen it so the Python API mounts. In **Settings → Plugins**, enable **ARGOS**; if the UI does not appear immediately, press **⌘K**, run **Reload desktop plugins**, then open **ARGOS: Open dashboard**. The default home is `~/.hermes`; custom profiles and `HERMES_HOME` are resolved automatically.

### Linux

```bash
hermes plugins install MichaelAchugwo/hermes-plugin-argos --enable --force
hermes gateway restart
hermes subs doctor
```

Restart Desktop or `hermes dashboard` after installation. Native Desktop hot-reloads JavaScript, but its Python backend must restart before `/api/plugins/argos` exists.

### Install from a local clone

```bash
git clone https://github.com/MichaelAchugwo/hermes-plugin-argos.git
cd hermes-plugin-argos
ARGOS_HOME="${HERMES_HOME:-$HOME/.hermes}"
rm -rf "$ARGOS_HOME/plugins/argos"
mkdir -p "$ARGOS_HOME/plugins"
cp -R . "$ARGOS_HOME/plugins/argos"
hermes plugins enable argos
hermes gateway restart
```

Hermes v0.21 accepts a Git URL, `owner/repo`, or community-index name in `hermes plugins install`—not a local directory—so local development uses an explicit copy plus `hermes plugins enable`.

The unified package installs under:

```text
$HERMES_HOME/plugins/argos/
├── plugin.yaml
├── __init__.py
├── hermes_argos/
├── dashboard/
├── desktop/plugin.js
└── tests/
```

ARGOS always resolves the active `$HERMES_HOME`; it does not hardcode `~/.hermes`.

## Add accounts

Repeat the official Hermes OAuth flow once per subscription:

```bash
hermes auth add openai-codex
```

For each subscription:

1. Run the command and complete the official browser/device-code flow.
2. Confirm the count increased with `hermes auth list openai-codex`.
3. Run `hermes subs list` and note the new non-secret fingerprint.
4. Repeat for the next subscription. If the OAuth page silently reuses the previous ChatGPT login, switch to the intended account there before approving. ARGOS never opens or scrapes `chatgpt.com` itself.
5. Finish with `hermes subs doctor` and `hermes subs refresh`.

Then check the pool:

```bash
hermes auth list openai-codex
hermes subs list
hermes subs status
```

Never copy tokens between entries manually. ARGOS rejects duplicate refresh-token fingerprints.

### Multi-subscription operating procedure

ARGOS pools **separate `openai-codex` OAuth credentials in Hermes**. It does
not merge subscriptions, modify the ChatGPT/Codex desktop application, scrape
the ChatGPT website, or bypass a plan limit. Each account must be one you are
authorized to use and must complete its own official OpenAI device login.

Use a clear label when adding each account:

```bash
hermes auth add openai-codex --label "Codex sub 2"
hermes auth add openai-codex --label "Codex sub 3"
```

The command opens the official device-login flow. In the terminal, open the
displayed OpenAI URL, enter the displayed one-time code, sign in to the
intended account, and approve. Do not paste device codes or OAuth tokens into
chat, configuration files, or a shell history.

After every addition, run:

```bash
hermes auth list openai-codex
hermes subs refresh
hermes subs status
hermes subs doctor
```

Interpret the result before expecting a failover:

- `ok` means both known quota windows are above the configured threshold and
  the account can be selected.
- `limited` means a relevant 5-hour or weekly window is empty or at threshold;
  ARGOS deliberately skips it.
- `unavailable` means the usage probe failed; `reauth` means the OAuth refresh
  needs to be completed again.

Automatic rotation is on by default. Verify or change it with:

```bash
hermes subs auto on
hermes subs current
hermes subs next             # manual fallback
hermes subs use "Codex sub 2" # explicit manual selection
```

When every account is limited, ARGOS leaves Hermes's configured fallback-model
policy in control; it cannot manufacture additional quota. A running agent can
rotate on a real cap error through Hermes core recovery, while new sessions
start from ARGOS's latest healthy ordering.

## CLI

```text
hermes subs                         status
hermes subs list                    redacted local pool listing
hermes subs current                 active subscription
hermes subs use <selector>          index | id | label | fingerprint prefix
hermes subs next                    manual fallback switch
hermes subs status                  quota and health for every account
hermes subs refresh                 force network refresh and apply policy
hermes subs keepalive --once        conservative serial OAuth refresh
hermes subs doctor                  validate auth and plugin surfaces
hermes subs auto on|off             persist automatic policy toggle
```

Place `--json` before the subcommand for machine-readable output:

```bash
hermes subs --json status
```

## How rotation works

Hermes v0.21.0 already owns the critical in-flight retry path:

- `usage_limit_reached` or a plan-cap 429 marks the issuing credential exhausted and rotates immediately.
- A generic 429 retries that credential once; the second 429 marks it exhausted and rotates.
- A 401 refreshes the exact failed OAuth entry; a failed refresh rotates.
- Exhaustion is persisted in `credential_pool.openai-codex[]`, so Ctrl+C does not erase it.
- When every entry is unavailable, Hermes's configured `fallback_model` remains authoritative.

ARGOS deliberately does not monkeypatch this tested core logic. It fetches each account's official usage windows, determines health, and persists priority order so Hermes core's stable `fill_first` selector uses the desired policy on new agents/sessions. A plugin-lifetime quota scheduler and Desktop/dashboard polling maintain that order. An already-running agent retains its in-memory pool until native core rotation or agent/session recreation.

Default `least_weekly_remaining` spends the healthy subscription nearest its weekly limit first, preserving fuller weeks as reserves.

## Configuration

`$HERMES_HOME/argos.yaml`:

```yaml
argos:
  auto_rotate: true
  strategy: least_weekly_remaining
  empty_threshold_pct: 1
  usage_cache_seconds: 5
  usage_poll_seconds: 5
  keepalive_enabled: true
  access_refresh_skew_minutes: 20
  keepalive_interval_hours: 6
```

- `least_weekly_remaining`: lowest healthy weekly balance first.
- `fill_first`: priority order only.
- `round_robin`: next healthy account during policy refresh.
- `usage_cache_seconds`: maximum age of an official usage snapshot before a normal status read refetches it. `5` permits five-second snapshots.
- `usage_poll_seconds`: scheduler cadence while the Hermes plugin backend is running. `5` checks every account against the official usage endpoint every five seconds and reapplies the healthy-account policy.

A five-second cadence intentionally makes more official quota requests. Use it only for a small, user-authorized pool and raise both values if the provider reports throttling. The scheduler never uses browser automation, website scraping, cookies, or device-login data.

## Desktop and dashboard

ARGOS contributes to native Desktop:

- a dockable right pane;
- a full `/argos` page and sidebar entry;
- a status-bar chip showing the active account's worst remaining window;
- hover detail for every account;
- Refresh, Use account, and Auto controls.

The web dashboard mounts the same backend under `/api/plugins/argos` and adds an ARGOS tab. Both Desktop surfaces poll every 5 seconds; cache and scheduler cadence are configured independently in `argos.yaml`. Refresh forces a live fetch.

### Desktop checklist

1. `hermes plugins list --plain --no-bundled` shows `argos` as `enabled`.
2. Restart Hermes Desktop so its owned Python backend remounts plugin APIs.
3. Open **Settings → Plugins** and enable the ARGOS Desktop half. Python-plugin enablement and Desktop-UI enablement are separate gates.
4. Run **ARGOS: Open dashboard** from the command palette.
5. Confirm the status chip, full page, and right-side dockable pane render.
6. Click **Refresh**, then **Use account** on a non-active card when at least two accounts exist.

### Web-dashboard checklist

```bash
hermes dashboard --stop
hermes dashboard
```

Open the local URL printed by Hermes and select **ARGOS**. Reloading dashboard JavaScript does not remount `plugin_api.py`; restart the dashboard process after Python changes.

### Troubleshooting

- **`hermes subs` is unknown:** ARGOS is disabled or the process predates installation. Enable it and start a new CLI process.
- **ARGOS UI exists but calls fail/404:** restart Desktop or `hermes dashboard`; reloading Desktop plugins refreshes JavaScript only.
- **Only one card appears:** only one credential is pooled; repeat `hermes auth add openai-codex` with the other subscription.
- **Two cards represent the same account:** `hermes subs doctor` reports duplicate refresh-token fingerprints. Reauthenticate the intended second account instead of editing tokens.
- **Account says `reauth`:** its refresh token was invalidated. Repeat the official OAuth flow; ARGOS keeps the entry for diagnosis rather than deleting it.
- **All accounts are limited:** ARGOS reports that state and leaves Hermes's `fallback_model` policy in control.
- **macOS plugin does not appear:** check the active home with `hermes config path`, quit with ⌘Q, reopen, and verify the Desktop plugin toggle.

## Usage source and privacy

ARGOS queries each pool entry independently at:

```text
https://chatgpt.com/backend-api/wham/usage
```

It sends OAuth tokens only to HTTPS `chatgpt.com`, includes `ChatGPT-Account-Id` only when present in the token's claims, converts `used_percent` to remaining percentage, and caches responses at `$HERMES_HOME/cache/argos/usage.json` for five minutes. One failed account becomes `unavailable`; sibling cards remain visible.

API/CLI/UI output contains no raw `access_token`, `refresh_token`, `id_token`, cookie, or API key. Fingerprints are truncated SHA-256 values. Labels that resemble emails are masked in UI.

## Auth-store safety

Before every auth write, ARGOS:

1. acquires the same cross-process `auth.lock` Hermes core uses;
2. verifies only `providers.openai-codex` and `credential_pool.openai-codex` changed;
3. copies the prior file to `$HERMES_HOME/backups/argos/auth-<UTC>.json`;
4. retains the newest 20 backups;
5. writes an fsynced temporary file and commits with `os.replace`.

Manual `use` preserves error/cooldown state. Policy activation clears stale status only when usage proves the selected account healthy. A rate-limited pool is reported as limited, not logged out.

## Keepalive

At session start, ARGOS runs policy and keepalive in a throttled background worker. Keepalive refreshes one account at a time when its access JWT is within the configured skew or the slow interval has elapsed. It calls Hermes's existing `refresh_codex_oauth_pure`; it does not invent another OAuth client id. Rotated refresh tokens are persisted immediately.

A terminal refresh failure marks the entry reauthentication-required without deleting it. Reauthorize with:

```bash
hermes auth add openai-codex
```

For machines with few Hermes sessions, schedule `hermes subs keepalive --once` every six hours using Windows Task Scheduler or cron. Hermes's scheduler can run a wrapper under `$HERMES_HOME/scripts/` with `hermes cron create "every 6h" --script <wrapper> --no-agent`.

## Development

```bash
uv run --with pytest python -m pytest -q tests/test_argos.py
hermes plugins doctor . --ci
node --check desktop/plugin.js
node --check dashboard/dist/index.js
```

Tests use fake fixture tokens and make no live network calls.

## Uninstall

```bash
hermes plugins disable argos
hermes plugins remove argos
```

Optional state can be removed separately:

```text
$HERMES_HOME/argos.yaml
$HERMES_HOME/cache/argos/
$HERMES_HOME/backups/argos/
```

Uninstalling ARGOS does not remove OAuth credentials from `auth.json`.

## License

MIT. See [LICENSE](LICENSE).
