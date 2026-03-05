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

if TYPE_CHECKING:
    from blinkpy.blinkpy import Blink
    from blinkpy.sync_module import LocalStorageMediaItem

_LOGGER = logging.getLogger(__name__)


@dataclass
class CleanupResult:
    """Outcome of a cleanup run."""

    deleted: list[int] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)
    skipped_dry_run: int = 0

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
