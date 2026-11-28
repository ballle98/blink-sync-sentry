"""Delete and archive orchestration for local-storage clips.

Handles dry-run guarding, optional archiving before deletion, and
rate-limiting between API calls.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from blinkpy import api

from blink_sync_sentry.storage import (
    fetch_manifest,
    fetch_storage_status,
    request_purge_local_storage,
)

if TYPE_CHECKING:
    from blinkpy.blinkpy import Blink
    from blinkpy.sync_module import BlinkSyncModule, LocalStorageMediaItem

_LOGGER = logging.getLogger(__name__)


@dataclass
class CleanupResult:
    """Outcome of a cleanup run."""

    deleted: list[int] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)
    skipped_dry_run: int = 0
    still_present: list[int] = field(default_factory=list)

    @property
    def total_attempted(self) -> int:
        return len(self.deleted) + len(self.failed) + self.skipped_dry_run

    def as_dict(self) -> dict:
        return {
            "deleted_count": len(self.deleted),
            "archived_count": len(self.archived),
            "failed_count": len(self.failed),
            "skipped_dry_run": self.skipped_dry_run,
            "deleted_ids": self.deleted,
            "failed_ids": self.failed,
            "archived_files": self.archived,
            "still_present_ids": self.still_present,
        }


@dataclass
class PurgeResult:
    """Outcome of an all-clips purge."""

    skipped_dry_run: bool = False
    command_accepted: bool = False
    usage_before: int | None = None
    usage_after: int | None = None
    verified: bool = False
    archived: list[str] = field(default_factory=list)
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "skipped_dry_run": self.skipped_dry_run,
            "command_accepted": self.command_accepted,
            "usage_before": self.usage_before,
            "usage_after": self.usage_after,
            "verified": self.verified,
            "archived_count": len(self.archived),
            "archived_files": self.archived,
            "note": self.note,
        }


async def archive_clip(
    blink: Blink,
    clip: LocalStorageMediaItem,
    archive_dir: Path,
) -> str | None:
    """Download a clip to *archive_dir* before deletion.

    Returns the written file path on success, or ``None`` on failure.
    """
    ts = clip.created_at.strftime("%Y%m%d_%H%M%S")
    safe_name = clip.name.replace(" ", "_").replace("/", "_")
    filename = f"{safe_name}_{ts}_{clip.id}.mp4"
    dest = archive_dir / filename

    _LOGGER.info("Archiving clip %d → %s", clip.id, dest)
    try:
        # Create archive directory with error handling
        try:
            archive_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _LOGGER.error("Failed to create archive directory %s: %s", archive_dir, exc)
            return None
        
        await clip.prepare_download(blink)
        ok = await clip.download_video(blink, str(dest))
        if ok:
            _LOGGER.info("Archived clip %d successfully.", clip.id)
            return str(dest)
        _LOGGER.warning("Download returned False for clip %d.", clip.id)
    except Exception:
        _LOGGER.exception("Failed to archive clip %d.", clip.id)
    return None


async def delete_clip(
    blink: Blink,
    clip: LocalStorageMediaItem,
) -> bool:
    """Delete a single clip from the sync module's local storage."""
    _LOGGER.info(
        "Deleting clip %d (%s, %s).",
        clip.id,
        clip.name,
        clip.created_at.isoformat(),
    )
    try:
        ok = await clip.delete_video(blink)
        if ok:
            _LOGGER.info("Deleted clip %d.", clip.id)
        else:
            _LOGGER.warning("delete_video returned False for clip %d.", clip.id)
        return ok
    except Exception:
        _LOGGER.exception("Failed to delete clip %d.", clip.id)
        return False


async def run_cleanup(
    blink: Blink,
    clips: Sequence[LocalStorageMediaItem],
    *,
    force: bool = False,
    archive_dir: str | None = None,
    request_delay: float = 2.0,
) -> CleanupResult:
    """Execute the cleanup pipeline on *clips*.

    :param blink: Authenticated Blink instance.
    :param clips: Clips selected for deletion.
    :param force: If False (default), no deletes happen (dry-run).
    :param archive_dir: If set, download clips here before deleting.
    :param request_delay: Seconds to sleep between operations.
    :returns: Summary of what happened.
    """
    result = CleanupResult()

    if not clips:
        _LOGGER.info("No clips to process.")
        return result

    if not force:
        _LOGGER.info(
            "DRY RUN: would delete %d clip(s). Use --force to actually delete.",
            len(clips),
        )
        result.skipped_dry_run = len(clips)
        return result

    for i, clip in enumerate(clips):
        _LOGGER.info(
            "[%d/%d] Processing clip %d (%s, %s, %s bytes).",
            i + 1,
            len(clips),
            clip.id,
            clip.name,
            clip.created_at.isoformat(),
            clip.size,
        )

        # Archive first if requested
        if archive_dir:
            path = await archive_clip(blink, clip, Path(archive_dir))
            if path:
                result.archived.append(path)
            else:
                _LOGGER.warning(
                    "Archive failed for clip %d — skipping deletion to be safe.",
                    clip.id,
                )
                result.failed.append(clip.id)
                await asyncio.sleep(request_delay)
                continue

        # Delete
        ok = await delete_clip(blink, clip)
        if ok:
            result.deleted.append(clip.id)
        else:
            result.failed.append(clip.id)

        # Rate-limit
        if i < len(clips) - 1:
            await asyncio.sleep(request_delay)

    _LOGGER.info(
        "Cleanup complete: %d deleted, %d archived, %d failed.",
        len(result.deleted),
        len(result.archived),
        len(result.failed),
    )
    return result


async def purge_all_clips(
    blink: Blink,
    sync: BlinkSyncModule,
    *,
    force: bool = False,
    archive_dir: str | None = None,
) -> PurgeResult:
    """Purge every clip on the sync module's local storage.

    This uses the app's destructive ``DELETE .../local_storage`` route,
    which clears the drive even when the manifest is empty. It is guarded
    twice: this function requires *force*, and the CLI additionally requires
    an explicit ``--purge-all`` flag.

    If archiving is requested, every manifest clip must be archived before
    the purge is sent; one failure aborts the whole operation.

    The result is only marked verified when the module's reported
    ``usb_storage_used`` actually drops, or (when that field is
    unavailable) a fresh manifest is empty. An HTTP 200 alone is not
    evidence that anything was deleted (blinkpy #970).
    """
    result = PurgeResult()
    if not force:
        result.skipped_dry_run = True
        _LOGGER.info(
            "DRY RUN: would purge ALL local-storage clips. "
            "Use --force --purge-all to actually do it."
        )
        return result

    if archive_dir:
        for clip in await fetch_manifest(sync):
            path = await archive_clip(blink, clip, Path(archive_dir))
            if path is None:
                result.note = f"archiving clip {clip.id} failed; refusing to purge"
                _LOGGER.error(result.note)
                return result
            result.archived.append(path)

    before = await fetch_storage_status(sync)
    result.usage_before = before.used_percent if before else None

    response = await request_purge_local_storage(blink, sync)
    if response is None:
        result.note = "purge endpoint unavailable; nothing was deleted"
        _LOGGER.error(result.note)
        return result
    result.command_accepted = True

    if isinstance(response, dict) and response.get("id") and response.get("network_id"):
        await api.wait_for_command(blink, response)

    after = await fetch_storage_status(sync)
    result.usage_after = after.used_percent if after else None

    if result.usage_before is not None and result.usage_after is not None:
        if result.usage_before <= 1:
            result.verified = True
            result.note = "drive was already near empty before the purge"
        else:
            result.verified = result.usage_after < result.usage_before
            if not result.verified:
                result.note = (
                    f"reported usage did not drop "
                    f"({result.usage_before}% -> {result.usage_after}%)"
                )
    else:
        remaining = await fetch_manifest(sync)
        result.verified = not remaining
        if remaining:
            result.note = f"{len(remaining)} clip(s) still in the manifest"

    if result.verified:
        _LOGGER.info("Purge verified: local storage is clear.")
    else:
        _LOGGER.error("Purge could not be verified: %s", result.note)
    return result
