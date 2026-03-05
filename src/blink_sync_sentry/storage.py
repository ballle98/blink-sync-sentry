"""Core logic for interacting with Blink Sync Module local storage.

Handles manifest retrieval, clip filtering by retention policy, and
computing inventory statistics.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from blinkpy.blinkpy import Blink
    from blinkpy.sync_module import BlinkSyncModule, LocalStorageMediaItem

_LOGGER = logging.getLogger(__name__)


@dataclass
class SyncModuleInfo:
    """Summary of a sync module and its local-storage status."""

    name: str
    sync_id: int
    network_id: int
    serial: str | None
    status: str
    local_storage_enabled: bool
    local_storage_compatible: bool
    local_storage_active: bool
    account_name: str = "default"


@dataclass
class ClipStats:
    """Aggregate statistics for a set of local-storage clips."""

    count: int
    total_bytes: int
    oldest: datetime.datetime | None
    newest: datetime.datetime | None

    @property
    def total_mb(self) -> float:
        return self.total_bytes / (1024 * 1024)

    @property
    def total_gb(self) -> float:
        return self.total_bytes / (1024 * 1024 * 1024)

    def as_dict(self) -> dict:
        return {
            "count": self.count,
            "total_bytes": self.total_bytes,
            "total_mb": round(self.total_mb, 2),
            "total_gb": round(self.total_gb, 4),
            "oldest": self.oldest.isoformat() if self.oldest else None,
            "newest": self.newest.isoformat() if self.newest else None,
        }


def list_sync_modules(
    blink: Blink,
    account_name: str = "default",
) -> list[SyncModuleInfo]:
    """Return information about every sync module on the account."""
    results: list[SyncModuleInfo] = []
    for name, sync in blink.sync.items():
        ls = sync._local_storage
        results.append(
            SyncModuleInfo(
                name=name,
                sync_id=sync.sync_id,
                network_id=sync.network_id,
                serial=sync.serial,
                status=sync.status,
                local_storage_enabled=ls.get("enabled", False),
                local_storage_compatible=ls.get("compatible", False),
                local_storage_active=ls.get("status", False),
                account_name=account_name,
            )
        )
    return results


async def fetch_manifest(sync: BlinkSyncModule) -> list[LocalStorageMediaItem]:
    """Request and return the local-storage manifest clip list.

    Triggers a manifest refresh on the sync module, then returns the
    sorted list of ``LocalStorageMediaItem`` objects.
    """
    _LOGGER.info("Requesting local storage manifest for '%s' …", sync.name)
    await sync.update_local_storage_manifest()
    manifest = list(sync._local_storage["manifest"])
    _LOGGER.info("Manifest contains %d clip(s).", len(manifest))
    return manifest


def compute_stats(clips: Sequence[LocalStorageMediaItem]) -> ClipStats:
    """Compute aggregate statistics for a list of clips."""
    if not clips:
        return ClipStats(count=0, total_bytes=0, oldest=None, newest=None)
    # size is a string in blinkpy, convert to int
    total = sum(int(c.size) for c in clips)
    oldest = min(c.created_at for c in clips)
    newest = max(c.created_at for c in clips)
    return ClipStats(count=len(clips), total_bytes=total, oldest=oldest, newest=newest)


def estimate_storage_usage(total_bytes: int) -> dict:
    """Estimate storage usage percentage for common USB drive sizes."""
    total_gb = total_bytes / (1024 * 1024 * 1024)
    
    # Common USB drive sizes for Blink Sync Modules
    common_sizes = [16, 32, 64, 128, 256, 512]  # GB
    
    estimates = {}
    for size in common_sizes:
        if total_gb > 0:
            percent = (total_gb / size) * 100
            estimates[f"{size}GB"] = {
                "percent_used": round(percent, 2),
                "free_gb": round(size - total_gb, 2)
            }
        else:
            estimates[f"{size}GB"] = {
                "percent_used": 0.0,
                "free_gb": size
            }
    
    return estimates


def filter_clips_by_age(
    clips: Sequence[LocalStorageMediaItem],
    retention_days: int,
    now: datetime.datetime | None = None,
) -> list[LocalStorageMediaItem]:
    """Return clips older than *retention_days* days.

    :param clips: Full manifest clip list.
    :param retention_days: Number of days to keep.
    :param now: Override current time (for testing).
    :returns: List of clips that exceed the retention window.
    """
    if now is None:
        now = datetime.datetime.now(tz=datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=retention_days)
    expired = [c for c in clips if _ensure_tz(c.created_at) < cutoff]
    _LOGGER.debug(
        "Retention filter: cutoff=%s, %d of %d clips expired.",
        cutoff.isoformat(),
        len(expired),
        len(clips),
    )
    return expired


def filter_clips_by_usage(
    clips: Sequence[LocalStorageMediaItem],
    max_usage_gb: float,
) -> list[LocalStorageMediaItem]:
    """Return the oldest clips that must be deleted to get total usage ≤ *max_usage_gb*.

    Clips are processed newest-first (kept) until the budget is exhausted;
    the remainder (oldest) are returned for deletion.
    """
    max_bytes = max_usage_gb * 1024 * 1024 * 1024
    total = sum(int(c.size) for c in clips)
    if total <= max_bytes:
        _LOGGER.debug(
            "Usage %.2f GB ≤ limit %.2f GB — nothing to delete.",
            total / (1024**3),
            max_usage_gb,
        )
        return []

    # Sort newest-first; keep clips until budget exhausted.
    sorted_clips = sorted(clips, key=lambda c: c.created_at, reverse=True)
    kept_bytes = 0
    keep_count = 0
    for clip in sorted_clips:
        if kept_bytes + int(clip.size) <= max_bytes:
            kept_bytes += int(clip.size)
            keep_count += 1
        else:
            break

    to_delete = sorted_clips[keep_count:]
    _LOGGER.debug(
        "Usage filter: total=%.2f GB, limit=%.2f GB, keeping %d, deleting %d.",
        total / (1024**3),
        max_usage_gb,
        keep_count,
        len(to_delete),
    )
    return to_delete


def select_clips_for_deletion(
    clips: Sequence[LocalStorageMediaItem],
    retention_days: int | None = None,
    max_usage_gb: float | None = None,
    now: datetime.datetime | None = None,
) -> list[LocalStorageMediaItem]:
    """Apply configured retention policy and return clips to delete.

    If both *retention_days* and *max_usage_gb* are set, the **union** of
    both filter results is returned (i.e. a clip is deleted if *either*
    policy says so).
    """
    by_age: set[int] = set()
    by_usage: set[int] = set()

    if retention_days is not None:
        for c in filter_clips_by_age(clips, retention_days, now=now):
            by_age.add(c.id)

    if max_usage_gb is not None:
        for c in filter_clips_by_usage(clips, max_usage_gb):
            by_usage.add(c.id)

    delete_ids = by_age | by_usage
    if not delete_ids:
        return []

    return [c for c in clips if c.id in delete_ids]


def _ensure_tz(dt: datetime.datetime) -> datetime.datetime:
    """Return *dt* with UTC timezone if it is naive, or convert to UTC if timezone-aware."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.timezone.utc)
    else:
        # Convert any timezone-aware datetime to UTC for consistent comparison
        return dt.astimezone(datetime.timezone.utc)
