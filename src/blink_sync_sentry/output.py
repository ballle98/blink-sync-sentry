"""Output formatting: plain text, JSON, and optional Rich pretty-printing."""

from __future__ import annotations

import json
import sys
from typing import Any, Sequence

from blink_sync_sentry.cleanup import CleanupResult
from blink_sync_sentry.storage import ClipStats, SyncModuleInfo

try:
    from rich.console import Console
    from rich.table import Table

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False


def _print_json(data: Any) -> None:
    """Dump *data* as indented JSON to stdout."""
    print(json.dumps(data, indent=2, default=str))


# ---------------------------------------------------------------------------
# list subcommand
# ---------------------------------------------------------------------------

def format_sync_modules(
    modules: Sequence[SyncModuleInfo],
    *,
    use_json: bool = False,
) -> None:
    """Print sync module information."""
    if use_json:
        _print_json([_sync_info_dict(m) for m in modules])
        return

    if RICH_AVAILABLE:
        _rich_sync_modules(modules)
        return

    for m in modules:
        local = "active" if m.local_storage_active else "inactive"
        compat = "yes" if m.local_storage_compatible else "no"
        enabled = "yes" if m.local_storage_enabled else "no"
        print(
            f"  [{m.account_name}] {m.name}  (id={m.sync_id}, network={m.network_id}, "
            f"serial={m.serial}, status={m.status})\n"
            f"    Local storage: {local}  enabled={enabled}  compatible={compat}"
        )


def _sync_info_dict(m: SyncModuleInfo) -> dict:
    return {
        "account": m.account_name,
        "name": m.name,
        "sync_id": m.sync_id,
        "network_id": m.network_id,
        "serial": m.serial,
        "status": m.status,
        "local_storage_enabled": m.local_storage_enabled,
        "local_storage_compatible": m.local_storage_compatible,
        "local_storage_active": m.local_storage_active,
    }


def _rich_sync_modules(modules: Sequence[SyncModuleInfo]) -> None:
    console = Console()
    table = Table(title="Sync Modules")
    table.add_column("Account", style="cyan")
    table.add_column("Name", style="bold")
    table.add_column("ID")
    table.add_column("Network")
    table.add_column("Status")
    table.add_column("Local Storage")
    for m in modules:
        ls = "[green]active[/]" if m.local_storage_active else "[dim]inactive[/]"
        table.add_row(m.account_name, str(m.name), str(m.sync_id), str(m.network_id), m.status, ls)
    console.print(table)


# ---------------------------------------------------------------------------
# status subcommand
# ---------------------------------------------------------------------------

def format_clip_stats(
    sync_name: str,
    stats: ClipStats,
    *,
    account_name: str = "",
    use_json: bool = False,
    show_storage_estimates: bool = False,
    storage_capacity_gb: int | None = None,
) -> None:
    """Print clip inventory statistics."""
    from .storage import estimate_storage_usage
    
    if use_json:
        data = {"account": account_name, "sync_module": sync_name, **stats.as_dict()}
        if storage_capacity_gb:
            total_gb = stats.total_bytes / (1024 * 1024 * 1024)
            percent_used = (total_gb / storage_capacity_gb) * 100
            data["storage"] = {
                "capacity_gb": storage_capacity_gb,
                "used_gb": round(total_gb, 4),
                "percent_used": round(percent_used, 2),
                "free_gb": round(storage_capacity_gb - total_gb, 4)
            }
        elif show_storage_estimates:
            data["storage_estimates"] = estimate_storage_usage(stats.total_bytes)
        _print_json(data)
        return

    header = f"Sync module: {sync_name}"
    if account_name:
        header = f"[{account_name}] {header}"
    print(header)
    print(f"  Clips:      {stats.count}")
    print(f"  Total size: {stats.total_mb:.2f} MB ({stats.total_gb:.4f} GB)")
    if stats.oldest:
        print(f"  Oldest:     {stats.oldest.isoformat()}")
    if stats.newest:
        print(f"  Newest:     {stats.newest.isoformat()}")
    
    if storage_capacity_gb:
        total_gb = stats.total_bytes / (1024 * 1024 * 1024)
        percent_used = (total_gb / storage_capacity_gb) * 100
        free_gb = storage_capacity_gb - total_gb
        print(f"  Storage:    {storage_capacity_gb}GB drive")
        if percent_used < 0.01:
            percent_str = "<0.01"
        else:
            percent_str = f"{percent_used:.2f}"
        print(f"              {percent_str}% used ({free_gb:.1f} GB free)")
    elif show_storage_estimates:
        estimates = estimate_storage_usage(stats.total_bytes)
        print("  Storage estimates (USB drive size):")
        # Show estimates for smaller drives first, as they're more common for Blink
        for size in ["16GB", "32GB", "64GB", "128GB", "256GB", "512GB"]:
            if size in estimates:
                info = estimates[size]
                if info["percent_used"] > 0 or stats.total_bytes > 0:
                    # Show percentage with more precision for very small values
                    if info["percent_used"] < 0.01:
                        percent_str = "<0.01"
                    else:
                        percent_str = f"{info['percent_used']:.2f}"
                    print(f"    {size}: {percent_str}% used ({info['free_gb']:.1f} GB free)")


# ---------------------------------------------------------------------------
# clean subcommand
# ---------------------------------------------------------------------------

def format_cleanup_preview(
    clips: Sequence[Any],
    *,
    use_json: bool = False,
) -> None:
    """Show which clips would be deleted (dry-run output)."""
    if use_json:
        _print_json({
            "action": "dry_run",
            "clips_to_delete": len(clips),
            "clips": [
                {
                    "id": c.id,
                    "camera": c.name,
                    "created_at": c.created_at.isoformat(),
                    "size": c.size,
                }
                for c in clips
            ],
        })
        return

    print(f"Would delete {len(clips)} clip(s):")
    for c in clips:
        print(f"  [{c.id}] {c.name}  {c.created_at.isoformat()}  ({c.size} bytes)")


def format_cleanup_result(
    result: CleanupResult,
    *,
    use_json: bool = False,
) -> None:
    """Print cleanup outcome."""
    if use_json:
        _print_json(result.as_dict())
        return

    if result.skipped_dry_run:
        print(f"DRY RUN: {result.skipped_dry_run} clip(s) would be deleted.")
        return

    print(f"Deleted:  {len(result.deleted)}")
    if result.archived:
        print(f"Archived: {len(result.archived)}")
    if result.failed:
        print(f"Failed:   {len(result.failed)}")
        for fid in result.failed:
            print(f"  - clip {fid}", file=sys.stderr)
