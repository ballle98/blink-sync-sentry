"""Command-line interface for blink-sync-sentry."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Sequence

from blinkpy.blinkpy import Blink

from blink_sync_sentry import __version__
from blink_sync_sentry.auth import AuthError, close_blink, create_blink
from blink_sync_sentry.cleanup import CleanupResult, run_cleanup
from blink_sync_sentry.config import AccountConfig, AppConfig, RetentionConfig, build_config
from blink_sync_sentry.daemon import WatchDaemon
from blink_sync_sentry.output import (
    format_cleanup_preview,
    format_cleanup_result,
    format_clip_stats,
    format_sync_modules,
)
from blink_sync_sentry.storage import (
    compute_stats,
    fetch_manifest,
    list_sync_modules,
    select_clips_for_deletion,
)

_LOGGER = logging.getLogger("blink_sync_sentry")

# Exit codes
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_DRY_RUN = 2


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser."""
    parser = argparse.ArgumentParser(
        prog="blink-sync-sentry",
        description="Monitor and maintain Blink Sync Module local storage.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to YAML config file.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose (DEBUG) logging.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="use_json",
        help="Output in JSON format.",
    )
    parser.add_argument(
        "--no-prompt",
        action="store_true",
        help="Non-interactive mode (fail on 2FA instead of prompting).",
    )
    parser.add_argument(
        "--account",
        default=None,
        help="Operate on a specific account by name (default: all accounts).",
    )
    parser.add_argument(
        "--storage-estimates",
        action="store_true",
        help="Show storage usage estimates for common USB drive sizes.",
    )
    parser.add_argument(
        "--storage-capacity",
        type=int,
        metavar="GB",
        help="Specify the USB drive capacity in GB for exact usage percentage.",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # --- list ---
    sub.add_parser("list", help="List sync modules and local storage status.")

    # --- status ---
    status_p = sub.add_parser(
        "status", help="Show local-storage clip inventory for a sync module."
    )
    status_p.add_argument(
        "--sync-module",
        dest="sync_module_name",
        help="Name of the sync module to inspect.",
    )

    # --- clean ---
    clean_p = sub.add_parser(
        "clean", help="Delete old local-storage clips per retention policy."
    )
    clean_p.add_argument(
        "--sync-module",
        dest="sync_module_name",
        help="Name of the sync module to clean.",
    )
    clean_p.add_argument(
        "--retention-days",
        type=int,
        default=None,
        help="Delete clips older than N days.",
    )
    clean_p.add_argument(
        "--max-usage-gb",
        type=float,
        default=None,
        help="Delete oldest clips until total usage ≤ X GB.",
    )
    clean_p.add_argument(
        "--archive-dir",
        default=None,
        help="Download clips to this directory before deleting.",
    )
    clean_p.add_argument(
        "--force",
        action="store_true",
        help="Actually delete clips (default is dry-run).",
    )

    # --- watch ---
    watch_p = sub.add_parser(
        "watch",
        help="Run as a daemon, periodically cleaning local storage.",
    )
    watch_p.add_argument(
        "--sync-module",
        dest="sync_module_name",
        help="Name of the sync module to watch.",
    )
    watch_p.add_argument(
        "--retention-days",
        type=int,
        default=None,
        help="Delete clips older than N days.",
    )
    watch_p.add_argument(
        "--max-usage-gb",
        type=float,
        default=None,
        help="Delete oldest clips until total usage ≤ X GB.",
    )
    watch_p.add_argument(
        "--archive-dir",
        default=None,
        help="Download clips to this directory before deleting.",
    )
    watch_p.add_argument(
        "--interval-minutes",
        type=int,
        default=None,
        help="Minutes between cleanup cycles (default: 60).",
    )
    watch_p.add_argument(
        "--force",
        action="store_true",
        help="Required for watch mode (daemon must actually delete).",
    )

    return parser


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    )
    root = logging.getLogger("blink_sync_sentry")
    root.setLevel(level)
    root.addHandler(handler)


def _validate_args(args: argparse.Namespace) -> None:
    """Validate CLI arguments and raise ValueError for invalid values."""
    if args.command in ("clean", "watch"):
        if args.retention_days is not None and args.retention_days < 0:
            raise ValueError("--retention-days must be non-negative")
        if args.max_usage_gb is not None and args.max_usage_gb < 0:
            raise ValueError("--max-usage-gb must be non-negative")
    
    if args.command == "watch" and args.interval_minutes is not None and args.interval_minutes <= 0:
        raise ValueError("--interval-minutes must be positive")
    
    if args.command == "status" and args.storage_capacity is not None and args.storage_capacity <= 0:
        raise ValueError("--storage-capacity must be positive")


def _cli_overrides(args: argparse.Namespace) -> dict:
    overrides: dict = {}
    if args.no_prompt:
        overrides["no_prompt"] = True

    if args.command in ("clean", "watch"):
        retention: dict = {}
        if args.retention_days is not None:
            retention["retention_days"] = args.retention_days
        if args.max_usage_gb is not None:
            retention["max_usage_gb"] = args.max_usage_gb
        if retention:
            overrides["retention"] = retention

        if args.archive_dir is not None:
            overrides["archive_dir"] = args.archive_dir

    if args.command == "watch" and args.interval_minutes is not None:
        overrides["watch"] = {"interval_minutes": args.interval_minutes}

    if hasattr(args, "sync_module_name") and args.sync_module_name is not None:
        overrides["sync_module_name"] = args.sync_module_name

    return overrides


def _select_accounts(
    cfg: AppConfig,
    account_filter: str | None,
) -> list[AccountConfig]:
    """Return the accounts to operate on, filtered by --account if given."""
    if account_filter:
        matched = [a for a in cfg.accounts if a.name == account_filter]
        if not matched:
            names = [a.name for a in cfg.accounts]
            _LOGGER.error(
                "Account '%s' not found in config. Available: %s",
                account_filter,
                names,
            )
        return matched
    return list(cfg.accounts)


def _resolve_syncs(
    blink: Blink,
    acct: AccountConfig,
    cli_sync_name: str | None = None,
) -> list:
    """Return the list of BlinkSyncModule objects to operate on.

    Priority: CLI --sync-module > account config sync_module_names > all.
    """
    if cli_sync_name:
        names_to_find = [cli_sync_name]
    elif acct.sync_module_names:
        names_to_find = acct.sync_module_names
    else:
        # Use all sync modules that have active local storage.
        return [
            s for s in blink.sync.values()
            if s._local_storage.get("status", False)
        ]

    found = []
    for n in names_to_find:
        if n in blink.sync:
            found.append(blink.sync[n])
        else:
            _LOGGER.warning(
                "Account '%s': sync module '%s' not found. Available: %s",
                acct.name,
                n,
                list(blink.sync.keys()),
            )
    return found


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

async def _cmd_list(
    cfg: AppConfig,
    args: argparse.Namespace,
    accounts: Sequence[AccountConfig],
) -> int:
    all_modules = []
    for acct in accounts:
        try:
            blink = await create_blink(acct)
        except AuthError as exc:
            _LOGGER.error("%s", exc)
            continue
        try:
            all_modules.extend(list_sync_modules(blink, account_name=acct.name))
        finally:
            await close_blink(blink)
    format_sync_modules(all_modules, use_json=args.use_json)
    return EXIT_OK if all_modules else EXIT_ERROR


async def _cmd_status(
    cfg: AppConfig,
    args: argparse.Namespace,
    accounts: Sequence[AccountConfig],
) -> int:
    cli_sync = getattr(args, "sync_module_name", None)
    found_any = False
    for acct in accounts:
        try:
            blink = await create_blink(acct)
        except AuthError as exc:
            _LOGGER.error("%s", exc)
            continue
        try:
            syncs = _resolve_syncs(blink, acct, cli_sync)
            for sync in syncs:
                if not sync.local_storage:
                    _LOGGER.warning(
                        "[%s] Sync module '%s' does not have active local storage.",
                        acct.name,
                        sync.name,
                    )
                    continue
                clips = await fetch_manifest(sync)
                stats = compute_stats(clips)
                # Get storage capacity: CLI > config > None
                storage_capacity = args.storage_capacity or cfg.effective_storage_capacity(acct)
                format_clip_stats(
                    sync.name, stats, account_name=acct.name, use_json=args.use_json,
                    show_storage_estimates=args.storage_estimates,
                    storage_capacity_gb=storage_capacity
                )
                found_any = True
        finally:
            await close_blink(blink)
    return EXIT_OK if found_any else EXIT_ERROR


async def _cmd_clean(
    cfg: AppConfig,
    args: argparse.Namespace,
    accounts: Sequence[AccountConfig],
) -> int:
    cli_sync = getattr(args, "sync_module_name", None)
    force = args.force
    worst_code = EXIT_OK

    for acct in accounts:
        retention = cfg.effective_retention(acct)
        archive_dir = cfg.effective_archive_dir(acct)

        if retention.retention_days is None and retention.max_usage_gb is None:
            _LOGGER.error(
                "Account '%s': specify --retention-days and/or --max-usage-gb.",
                acct.name,
            )
            worst_code = EXIT_ERROR
            continue

        try:
            blink = await create_blink(acct)
        except AuthError as exc:
            _LOGGER.error("%s", exc)
            worst_code = EXIT_ERROR
            continue

        try:
            syncs = _resolve_syncs(blink, acct, cli_sync)
            for sync in syncs:
                if not sync.local_storage:
                    _LOGGER.warning(
                        "[%s] Sync module '%s' has no active local storage — skipping.",
                        acct.name,
                        sync.name,
                    )
                    continue

                clips = await fetch_manifest(sync)
                to_delete = select_clips_for_deletion(
                    clips,
                    retention_days=retention.retention_days,
                    max_usage_gb=retention.max_usage_gb,
                )

                if not to_delete:
                    _LOGGER.info(
                        "[%s/%s] No clips match the retention policy.",
                        acct.name,
                        sync.name,
                    )
                    if args.use_json:
                        format_cleanup_result(CleanupResult(), use_json=True)
                    continue

                if not force:
                    format_cleanup_preview(to_delete, use_json=args.use_json)
                    worst_code = max(worst_code, EXIT_DRY_RUN)
                    continue

                result = await run_cleanup(
                    blink,
                    to_delete,
                    force=True,
                    archive_dir=archive_dir,
                    request_delay=cfg.request_delay_seconds,
                )
                format_cleanup_result(result, use_json=args.use_json)
                if result.failed:
                    worst_code = max(worst_code, EXIT_ERROR)
        finally:
            await close_blink(blink)

    return worst_code


async def _cmd_watch(
    cfg: AppConfig,
    args: argparse.Namespace,
    accounts: Sequence[AccountConfig],
) -> int:
    if not args.force:
        _LOGGER.error("Watch mode requires --force (daemon must actually delete).")
        return EXIT_ERROR

    cli_sync = getattr(args, "sync_module_name", None)

    # Build list of (blink, sync, acct) tuples to watch.
    targets: list[tuple[Blink, object, AccountConfig]] = []
    blinks: list[Blink] = []

    for acct in accounts:
        retention = cfg.effective_retention(acct)
        if retention.retention_days is None and retention.max_usage_gb is None:
            _LOGGER.error(
                "Account '%s': specify --retention-days and/or --max-usage-gb.",
                acct.name,
            )
            continue

        try:
            blink = await create_blink(acct)
        except AuthError as exc:
            _LOGGER.error("%s", exc)
            continue

        blinks.append(blink)
        syncs = _resolve_syncs(blink, acct, cli_sync)
        for sync in syncs:
            if not sync.local_storage:
                _LOGGER.warning(
                    "[%s] Sync module '%s' has no active local storage — skipping.",
                    acct.name,
                    sync.name,
                )
                continue
            targets.append((blink, sync, acct))

    if not targets:
        _LOGGER.error("No valid sync modules found to watch.")
        for b in blinks:
            await close_blink(b)
        return EXIT_ERROR

    daemon = WatchDaemon(targets, cfg)
    try:
        await daemon.run()
    finally:
        for b in blinks:
            await close_blink(b)
    return EXIT_OK


_COMMAND_MAP = {
    "list": _cmd_list,
    "status": _cmd_status,
    "clean": _cmd_clean,
    "watch": _cmd_watch,
}


async def _async_main(args: argparse.Namespace, cfg: AppConfig) -> int:
    accounts = _select_accounts(cfg, args.account)
    if not accounts:
        return EXIT_ERROR

    handler = _COMMAND_MAP[args.command]
    return await handler(cfg, args, accounts)


def main(argv: list[str] | None = None) -> None:
    """Entry point for the console script."""
    parser = build_parser()
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)
    
    # Validate arguments
    try:
        _validate_args(args)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(EXIT_ERROR)

    overrides = _cli_overrides(args)
    cfg = build_config(config_path=args.config, cli_overrides=overrides)

    exit_code = asyncio.run(_async_main(args, cfg))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
