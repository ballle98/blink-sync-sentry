"""Tests for cleanup orchestration: dry-run, archive, delete."""

from __future__ import annotations

import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from blink_sync_sentry.cleanup import (
    CleanupResult,
    archive_clip,
    delete_clip,
    run_cleanup,
)


def _make_clip(
    clip_id: int,
    name: str = "FrontDoor",
    created_at: datetime.datetime | None = None,
    size: int = 1_000_000,
) -> MagicMock:
    """Create a mock LocalStorageMediaItem."""
    clip = MagicMock()
    clip.id = clip_id
    clip.name = name
    clip.created_at = created_at or datetime.datetime(
        2025, 6, 10, 12, 0, 0, tzinfo=datetime.timezone.utc
    )
    clip.size = size
    clip.prepare_download = AsyncMock(return_value=True)
    clip.download_video = AsyncMock(return_value=True)
    clip.delete_video = AsyncMock(return_value=True)
    return clip


@pytest.fixture()
def blink() -> MagicMock:
    return MagicMock()


class TestDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_deletes_nothing(self, blink: MagicMock) -> None:
        clips = [_make_clip(1), _make_clip(2)]
        result = await run_cleanup(blink, clips, force=False)
        assert result.skipped_dry_run == 2
        assert result.deleted == []
        assert result.failed == []
        for c in clips:
            c.delete_video.assert_not_called()

    @pytest.mark.asyncio
    async def test_dry_run_is_default(self, blink: MagicMock) -> None:
        clips = [_make_clip(1)]
        result = await run_cleanup(blink, clips)
        assert result.skipped_dry_run == 1

    @pytest.mark.asyncio
    async def test_empty_clips(self, blink: MagicMock) -> None:
        result = await run_cleanup(blink, [], force=True)
        assert result.total_attempted == 0


class TestForceDelete:
    @pytest.mark.asyncio
    async def test_delete_success(self, blink: MagicMock) -> None:
        clips = [_make_clip(1), _make_clip(2)]
        result = await run_cleanup(blink, clips, force=True, request_delay=0)
        assert result.deleted == [1, 2]
        assert result.failed == []

    @pytest.mark.asyncio
    async def test_delete_failure(self, blink: MagicMock) -> None:
        clip = _make_clip(1)
        clip.delete_video = AsyncMock(return_value=False)
        result = await run_cleanup(blink, [clip], force=True, request_delay=0)
        assert result.deleted == []
        assert result.failed == [1]


class TestArchiveBeforeDelete:
    @pytest.mark.asyncio
    async def test_archive_then_delete(
        self, blink: MagicMock, tmp_path: Path
    ) -> None:
        clips = [_make_clip(1)]
        result = await run_cleanup(
            blink, clips, force=True, archive_dir=str(tmp_path), request_delay=0
        )
        assert len(result.archived) == 1
        assert result.deleted == [1]
        clips[0].prepare_download.assert_called_once()
        clips[0].download_video.assert_called_once()

    @pytest.mark.asyncio
    async def test_archive_failure_skips_delete(
        self, blink: MagicMock, tmp_path: Path
    ) -> None:
        clip = _make_clip(1)
        clip.download_video = AsyncMock(return_value=False)
        result = await run_cleanup(
            blink, [clip], force=True, archive_dir=str(tmp_path), request_delay=0
        )
        assert result.archived == []
        assert result.deleted == []
        assert result.failed == [1]
        clip.delete_video.assert_not_called()


class TestCleanupResult:
    def test_as_dict(self) -> None:
        r = CleanupResult(deleted=[1, 2], archived=["/a.mp4"], failed=[3])
        d = r.as_dict()
        assert d["deleted_count"] == 2
        assert d["archived_count"] == 1
        assert d["failed_count"] == 1

    def test_total_attempted(self) -> None:
        r = CleanupResult(deleted=[1], failed=[2], skipped_dry_run=3)
        assert r.total_attempted == 5
