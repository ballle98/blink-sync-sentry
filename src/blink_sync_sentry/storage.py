"""Core logic for interacting with Blink Sync Module local storage.

Handles manifest retrieval, clip filtering by retention policy, and
computing inventory statistics.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Sequence

from blinkpy import api
from blinkpy.helpers.constants import TIMEOUT

if TYPE_CHECKING:
    from blinkpy.blinkpy import Blink
    from blinkpy.sync_module import BlinkSyncModule, LocalStorageMediaItem

_LOGGER = logging.getLogger(__name__)

STORAGE_PATH = (
    "/api/v1/accounts/{account_id}/networks/{network_id}"
    "/sync_modules/{sync_id}/local_storage"
)
STORAGE_STATUS_PATH = STORAGE_PATH + "/status"


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
class StorageStatus:
    """Usage and health status reported by a sync module.

    Sourced from the undocumented ``local_storage/status`` endpoint, which
    is what the Blink app uses to display "97% full". The drive percentage
    is measured by the module itself, not estimated from the manifest, so
    it stays correct when the manifest is empty or stale.
    """

    used_percent: int | None
    full: bool
    usb_state: str | None
    warning: int | None
    backup_enabled: bool
    backup_in_progress: bool
    last_backup_result: str | None
    last_backup_completed: str | None
    format_compatible: bool | None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> StorageStatus:
        """Build a status object from the raw endpoint response."""
        return cls(
            used_percent=payload.get("usb_storage_used"),
            full=bool(payload.get("usb_storage_full", False)),
            usb_state=payload.get("usb_state"),
            warning=payload.get("storage_warning"),
            backup_enabled=bool(payload.get("sm_backup_enabled", False)),
            backup_in_progress=bool(payload.get("sm_backup_in_progress", False)),
            last_backup_result=payload.get("last_backup_result"),
            last_backup_completed=payload.get("last_backup_completed"),
            format_compatible=payload.get("usb_format_compatible"),
            raw=payload,
        )

    @property
    def is_nearly_full(self) -> bool:
        """Return True when the module reports a full or ≥90% drive."""
        if self.full:
            return True
        return self.used_percent is not None and self.used_percent >= 90

    def as_dict(self) -> dict:
        return {
            "used_percent": self.used_percent,
            "full": self.full,
            "usb_state": self.usb_state,
            "storage_warning": self.warning,
            "backup_enabled": self.backup_enabled,
            "backup_in_progress": self.backup_in_progress,
            "last_backup_result": self.last_backup_result,
            "last_backup_completed": self.last_backup_completed,
            "format_compatible": self.format_compatible,
            "raw": self.raw,
        }


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

    If the refresh fails, the previous manifest is returned with a warning;
    an empty result must not be trusted as "the drive is empty" in that case.
    """
    _LOGGER.info("Requesting local storage manifest for '%s' …", sync.name)
    refreshed = await sync.update_local_storage_manifest()
    if not refreshed:
        _LOGGER.warning(
            "Manifest refresh failed for '%s' — results may be stale "
            "(this is not proof that the drive is empty).",
            sync.name,
        )
    manifest = list(sync._local_storage["manifest"])
    _LOGGER.info("Manifest contains %d clip(s).", len(manifest))
    return manifest


async def request_purge_local_storage(
    blink: Blink,
    sync: BlinkSyncModule,
) -> dict | None:
    """Send the app's destructive `DELETE .../local_storage` request.

    No guard is applied here; callers must have already checked `--force`
    and the explicit purge confirmation. Returns the command response, or
    ``None`` if the route is unavailable on this firmware/region.
    """
    url = blink.urls.base_url + STORAGE_PATH.format(
        account_id=blink.account_id,
        network_id=sync.network_id,
        sync_id=sync.sync_id,
    )
    _LOGGER.warning("Sending DELETE %s", url)
    response = await blink.auth.session.delete(
        url, headers=blink.auth.header, timeout=TIMEOUT
    )
    if response.status == 404:
        _LOGGER.warning("Purge endpoint is not available on this sync module.")
        return None
    return await blink.auth.validate_response(response, json_resp=True)


async def fetch_storage_status(sync: BlinkSyncModule) -> StorageStatus | None:
    """Read the drive usage status reported by the sync module.

    Returns ``None`` when local storage is inactive, or when the module,
    its firmware or the API region does not expose the endpoint (older
    blinkpy releases also lack a wrapper, so fall back to a direct GET).
    """
    if not sync.local_storage:
        return None

    updater = getattr(sync, "update_local_storage_status", None)
    if updater is None:
        blink = sync.blink
        url = blink.urls.base_url + STORAGE_STATUS_PATH.format(
            account_id=blink.account_id,
            network_id=sync.network_id,
            sync_id=sync.sync_id,
        )
        # blinkpy's query helper swallows 404s and returns None.
        payload = await api.http_get(blink, url)
    else:
        payload = await updater()

    if not payload:
        _LOGGER.debug("No local storage status reported for '%s'.", sync.name)
        return None
    return StorageStatus.from_payload(payload)


async def verify_deletions(
    sync: BlinkSyncModule,
    deleted_ids: Sequence[int],
) -> list[int]:
    """Return deleted clip IDs that are still listed in a fresh manifest.

    The delete endpoint returns HTTP 200 even when nothing was removed
    (blinkpy#970), so a successful *response* is not proof of deletion.
    Re-reading the manifest is the only way to confirm.

    Returns an empty list when verification is impossible (no IDs, or the
    manifest refresh failed), since an unverifiable result must not be
    reported as a failed deletion.
    """
    if not deleted_ids:
        return []

    refreshed = await sync.update_local_storage_manifest()
    if not refreshed:
        _LOGGER.warning(
            "Could not refresh the manifest for '%s' — skipping delete verification.",
            sync.name,
        )
        return []

    present = {clip.id for clip in sync._local_storage["manifest"]}
    still_present = sorted(set(deleted_ids) & present)
    if still_present:
        _LOGGER.error(
            "[%s] %d clip(s) reported as deleted are still in the manifest: %s",
            sync.name,
            len(still_present),
            ", ".join(str(i) for i in still_present),
        )
    return still_present


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
