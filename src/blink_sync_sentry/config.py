"""Configuration loading from YAML file and environment variables.

Supports both single-account (flat) and multi-account (``accounts:`` list)
configuration layouts.  The single-account format is automatically normalized
into a one-element ``accounts`` list so all downstream code can treat the
config uniformly.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_LOGGER = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("~/.config/blink-sync-sentry/config.yaml").expanduser()
DEFAULT_TOKEN_DIR = Path("~/.config/blink-sync-sentry").expanduser()
DEFAULT_TOKEN_PATH = DEFAULT_TOKEN_DIR / "token.json"


@dataclass
class WatchConfig:
    """Settings for daemon / watch mode."""

    interval_minutes: int = 60


@dataclass
class RetentionConfig:
    """Retention policy settings."""

    retention_days: int | None = None
    max_usage_gb: float | None = None


@dataclass
class AccountConfig:
    """Credentials and targeting for a single Blink account."""

    name: str = "default"
    username: str = ""
    password: str = ""
    token_file: str = str(DEFAULT_TOKEN_PATH)
    no_prompt: bool = False

    # Per-account targeting — which sync modules to manage.
    # If empty, all sync modules with active local storage are used.
    sync_module_names: list[str] = field(default_factory=list)

    # Per-account retention (overrides top-level if set).
    retention: RetentionConfig = field(default_factory=RetentionConfig)

    # Per-account archive dir (overrides top-level if set).
    archive_dir: str | None = None

    # Per-account storage capacity (overrides top-level if set).
    storage_capacity_gb: int | None = None


@dataclass
class AppConfig:
    """Top-level application configuration."""

    # Accounts
    accounts: list[AccountConfig] = field(default_factory=list)

    # Global defaults (applied when per-account values are not set)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    archive_dir: str | None = None
    storage_capacity_gb: int | None = None

    # Watch / daemon
    watch: WatchConfig = field(default_factory=WatchConfig)

    # Rate limiting
    request_delay_seconds: float = 2.0

    # Global flags (set by CLI)
    no_prompt: bool = False

    def effective_retention(self, acct: AccountConfig) -> RetentionConfig:
        """Return the retention policy for *acct*, falling back to global."""
        return RetentionConfig(
            retention_days=(
                acct.retention.retention_days
                if acct.retention.retention_days is not None
                else self.retention.retention_days
            ),
            max_usage_gb=(
                acct.retention.max_usage_gb
                if acct.retention.max_usage_gb is not None
                else self.retention.max_usage_gb
            ),
        )

    def effective_archive_dir(self, acct: AccountConfig) -> str | None:
        """Return the archive dir for *acct*, falling back to global."""
        return acct.archive_dir if acct.archive_dir is not None else self.archive_dir

    def effective_storage_capacity(self, acct: AccountConfig) -> int | None:
        """Return the storage capacity in GB for *acct*, falling back to global."""
        return acct.storage_capacity_gb if acct.storage_capacity_gb is not None else self.storage_capacity_gb


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge *override* into *base* recursively (mutates *base*)."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _env_overrides() -> dict[str, Any]:
    """Build a partial config dict from environment variables.

    Environment variables populate the top-level (single-account) fields.
    When using multi-account config, use per-account env vars like
    ``BLINK_<ACCOUNT>_USERNAME`` or put credentials in the YAML.
    """
    overrides: dict[str, Any] = {}

    if val := os.environ.get("BLINK_USERNAME"):
        overrides["username"] = val
    if val := os.environ.get("BLINK_PASSWORD"):
        overrides["password"] = val
    if val := os.environ.get("BLINK_TOKEN_FILE"):
        overrides["token_file"] = val

    return overrides


def _env_overrides_for_account(account_name: str) -> dict[str, Any]:
    """Build per-account overrides from ``BLINK_<NAME>_*`` env vars."""
    prefix = f"BLINK_{account_name.upper()}_"
    overrides: dict[str, Any] = {}
    if val := os.environ.get(f"{prefix}USERNAME"):
        overrides["username"] = val
    if val := os.environ.get(f"{prefix}PASSWORD"):
        overrides["password"] = val
    if val := os.environ.get(f"{prefix}TOKEN_FILE"):
        overrides["token_file"] = val
    return overrides


def load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML config file, returning an empty dict if missing."""
    if not path.is_file():
        _LOGGER.debug("Config file not found: %s", path)
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data if isinstance(data, dict) else {}


def _parse_retention(raw: dict[str, Any] | None) -> RetentionConfig:
    if not raw:
        return RetentionConfig()
    return RetentionConfig(
        retention_days=raw.get("retention_days"),
        max_usage_gb=raw.get("max_usage_gb"),
    )


def _parse_account(raw: dict[str, Any], global_no_prompt: bool) -> AccountConfig:
    """Parse a single account entry from config."""
    name = raw.get("name", "default")
    env = _env_overrides_for_account(name)
    merged = _deep_merge(dict(raw), env)

    sync_names_raw = merged.get("sync_module_names") or merged.get("sync_module_name")
    if isinstance(sync_names_raw, str):
        sync_names = [sync_names_raw]
    elif isinstance(sync_names_raw, list):
        sync_names = list(sync_names_raw)
    else:
        sync_names = []

    return AccountConfig(
        name=name,
        username=merged.get("username", ""),
        password=merged.get("password", ""),
        token_file=merged.get(
            "token_file",
            str(DEFAULT_TOKEN_DIR / f"token_{name}.json"),
        ),
        no_prompt=bool(merged.get("no_prompt", global_no_prompt)),
        sync_module_names=sync_names,
        retention=_parse_retention(merged.get("retention")),
        archive_dir=merged.get("archive_dir"),
        storage_capacity_gb=merged.get("storage_capacity_gb"),
    )


def build_config(
    config_path: Path | None = None,
    cli_overrides: dict[str, Any] | None = None,
) -> AppConfig:
    """Build an AppConfig by layering: defaults < YAML file < env vars < CLI args.

    Supports two YAML layouts:

    **Single-account (flat)** — ``username``, ``password``, ``token_file``,
    ``sync_module_name`` at the top level.  Normalized into a one-element
    ``accounts`` list.

    **Multi-account** — an ``accounts:`` list where each entry has its own
    credentials, token file, and sync module targets.

    :param config_path: Path to a YAML config file (or None to use default).
    :param cli_overrides: Dict of values supplied on the command line.
    :returns: Fully resolved AppConfig.
    """
    path = config_path or DEFAULT_CONFIG_PATH
    file_data = load_yaml(path)
    env_data = _env_overrides()
    cli_data = cli_overrides or {}

    merged = _deep_merge(file_data, env_data)
    merged = _deep_merge(merged, cli_data)

    # Global settings
    global_retention = _parse_retention(merged.get("retention"))
    watch_raw = merged.get("watch", {})
    watch = WatchConfig(
        interval_minutes=int(watch_raw.get("interval_minutes", 60)),
    )
    global_no_prompt = bool(merged.get("no_prompt", False))
    global_archive_dir = merged.get("archive_dir")
    global_storage_capacity = merged.get("storage_capacity_gb")
    request_delay = float(merged.get("request_delay_seconds", 2.0))

    # Parse accounts
    accounts_raw = merged.get("accounts")
    if accounts_raw and isinstance(accounts_raw, list):
        accounts = [_parse_account(a, global_no_prompt) for a in accounts_raw]
    else:
        # Single-account (flat) layout — synthesize from top-level fields.
        acct_raw: dict[str, Any] = {"name": "default"}
        for key in (
            "username", "password", "token_file", "no_prompt",
            "sync_module_name", "sync_module_names", "retention",
            "archive_dir", "network_name", "storage_capacity_gb",
        ):
            if key in merged:
                acct_raw[key] = merged[key]
        accounts = [_parse_account(acct_raw, global_no_prompt)]

    cfg = AppConfig(
        accounts=accounts,
        retention=global_retention,
        archive_dir=global_archive_dir,
        storage_capacity_gb=global_storage_capacity,
        watch=watch,
        request_delay_seconds=request_delay,
        no_prompt=global_no_prompt,
    )

    _LOGGER.debug("Loaded config: %s", cfg)
    return cfg
