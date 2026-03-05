# blink-sync-sentry

Monitor Blink Sync Module local storage and automatically clean up old video clips on the USB drive attached to your sync module.

Built on top of [blinkpy](https://github.com/fronzbot/blinkpy).

> **⚠ Safety first** — The default mode is **dry-run**. No clips are deleted
> unless you pass `--force`. Only *local-storage* clips on the sync module are
> ever touched; cloud clips are never modified.

---

## Features

- **List** sync modules and their local-storage status.
- **Inspect** clip inventory (count, total size, oldest/newest).
- **Clean** clips by age (`--retention-days`) or total size (`--max-usage-gb`).
- **Watch** (daemon) mode — run periodically to keep storage within bounds.
- **Archive** clips locally before deletion (`--archive-dir`).
- **Dry-run** by default; `--force` required to actually delete.
- **JSON output** (`--json`) for scripting; meaningful exit codes.
- Optional **rich** pretty-printing (install with `pip install blink-sync-sentry[rich]`).

## Installation

Requires **Python 3.11+**.

```bash
# From the repo
pip install .

# With optional rich output
pip install '.[rich]'

# Development
pip install -e '.[dev]'
```

After installation, the `blink-sync-sentry` command is available.

## Configuration

### Credentials

Credentials come from environment variables — **never put them in config files**.

**Single account:**

```bash
export BLINK_USERNAME="you@example.com"
export BLINK_PASSWORD="your-password"
```

**Multi-account** — prefix env vars with `BLINK_<ACCOUNT_NAME>_`:

```bash
export BLINK_HOME_USERNAME="home@example.com"
export BLINK_HOME_PASSWORD="home-password"
export BLINK_OFFICE_USERNAME="office@example.com"
export BLINK_OFFICE_PASSWORD="office-password"
```

On first run, you will be prompted for a 2FA PIN (sent to your email). After
successful authentication, a token file is saved so subsequent runs (including
daemon mode) don't need interactive login.

### Config file

Copy the example config and edit:

```bash
mkdir -p ~/.config/blink-sync-sentry
cp examples/config.yaml ~/.config/blink-sync-sentry/config.yaml
```

Or specify a custom path: `--config /path/to/config.yaml`.

See [`examples/config.yaml`](examples/config.yaml) for all options.

### Single-account config (simplest)

```yaml
sync_module_name: MySyncModule
retention:
  retention_days: 14
watch:
  interval_minutes: 60
```

### Multi-account config

Manage sync modules across multiple Blink accounts. Each account gets its own
credentials, token file, and sync module list. Per-account `retention` and
`archive_dir` override the global defaults.

```yaml
# Global defaults
retention:
  retention_days: 30
archive_dir: /mnt/nas/blink-archive

accounts:
  - name: home
    token_file: ~/.config/blink-sync-sentry/token_home.json
    sync_module_names:
      - FrontYard
      - BackYard
    retention:
      retention_days: 7
    archive_dir: /mnt/nas/blink-home

  - name: office
    token_file: ~/.config/blink-sync-sentry/token_office.json
    sync_module_names:
      - Lobby
    retention:
      max_usage_gb: 2.0
```

### Key settings

| Setting | Default | Description |
|---|---|---|
| `retention.retention_days` | — | Delete clips older than N days |
| `retention.max_usage_gb` | — | Delete oldest clips until usage ≤ X GB |
| `watch.interval_minutes` | `60` | Minutes between daemon cleanup cycles |
| `archive_dir` | — | Download clips here before deleting |
| `token_file` | `~/.config/blink-sync-sentry/token.json` | Auth token path |
| `request_delay_seconds` | `2.0` | Rate-limit delay between API calls |
| `accounts[].name` | `default` | Account label (used in output and env var prefix) |
| `accounts[].sync_module_names` | all | Sync modules to manage for this account |

## Usage

### List sync modules

```bash
# All accounts
blink-sync-sentry list
blink-sync-sentry list --json

# Single account only
blink-sync-sentry list --account home
```

### Show clip inventory

```bash
blink-sync-sentry status
blink-sync-sentry status --sync-module "My Sync Module"
blink-sync-sentry status --account office
```

### Clean old clips (dry-run)

```bash
# See what would be deleted (default: dry-run)
blink-sync-sentry clean --retention-days 14

# By size threshold
blink-sync-sentry clean --max-usage-gb 4.0

# Both policies (union: delete if EITHER matches)
blink-sync-sentry clean --retention-days 14 --max-usage-gb 4.0
```

### Clean old clips (actually delete)

```bash
blink-sync-sentry clean --retention-days 14 --force
```

### Archive before deleting

```bash
blink-sync-sentry clean --retention-days 14 --force --archive-dir /mnt/nas/blink-archive
```

### Watch (daemon) mode

```bash
blink-sync-sentry watch --retention-days 14 --max-usage-gb 4.0 --force
blink-sync-sentry watch --interval-minutes 30 --max-usage-gb 8.0 --force
```

Daemon mode requires `--force` and a pre-existing token file (run an
interactive session first to complete 2FA).

#### systemd example

For a single account:

```ini
[Unit]
Description=Blink Sync Sentry - local storage monitor
After=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/blink-sync-sentry watch --max-usage-gb 4.0 --force --no-prompt
Environment=BLINK_USERNAME=you@example.com
Environment=BLINK_PASSWORD=your-password
Restart=on-failure
RestartSec=60

[Install]
WantedBy=multi-user.target
```

For multi-account (watches all accounts defined in config):

```ini
[Service]
ExecStart=/usr/local/bin/blink-sync-sentry watch --force --no-prompt
Environment=BLINK_HOME_USERNAME=home@example.com
Environment=BLINK_HOME_PASSWORD=home-password
Environment=BLINK_OFFICE_USERNAME=office@example.com
Environment=BLINK_OFFICE_PASSWORD=office-password
```

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Error (auth failure, API error, etc.) |
| `2` | Dry-run — clips would have been deleted |

## Non-interactive mode

Pass `--no-prompt` to skip 2FA prompts. If 2FA is required and no valid token
file exists, the tool exits with code 1 and prints instructions for running an
interactive session first.

## Development

```bash
pip install -e '.[dev]'
pytest
```

## License

MIT