"""Tests for watch-mode deletion verification."""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from blink_sync_sentry.cleanup import CleanupResult
from blink_sync_sentry.daemon import WatchDaemon


def _make_clip(clip_id: int) -> MagicMock:
    clip = MagicMock()
    clip.id = clip_id
    clip.created_at = datetime.datetime(2025, 6, 1, tzinfo=datetime.UTC)
    clip.size = 1_000_000
    return clip


def _make_cfg() -> MagicMock:
    cfg = MagicMock()
    cfg.watch.interval_minutes = 60
    cfg.request_delay_seconds = 0
    cfg.effective_retention.return_value = MagicMock(
        retention_days=1, max_usage_gb=None
    )
    cfg.effective_archive_dir.return_value = None
    return cfg


class TestVerifyPreviousDeletions:
    def test_reports_clips_still_listed(self) -> None:
        daemon = WatchDaemon([], _make_cfg())
        daemon._pending["home/test"] = [1, 2]

        still = daemon._verify_previous_deletions("home/test", [_make_clip(2)])

        assert still == [2]
        assert "home/test" not in daemon._pending

    def test_all_gone(self) -> None:
        daemon = WatchDaemon([], _make_cfg())
        daemon._pending["home/test"] = [1, 2]

        assert daemon._verify_previous_deletions("home/test", []) == []

    def test_nothing_pending(self) -> None:
        daemon = WatchDaemon([], _make_cfg())
        assert daemon._verify_previous_deletions("home/test", [_make_clip(1)]) == []


class TestRunCycle:
    async def test_records_deleted_ids_for_next_cycle(self) -> None:
        sync = MagicMock()
        sync.name = "test"
        acct = MagicMock()
        acct.name = "home"
        daemon = WatchDaemon([(MagicMock(), sync, acct)], _make_cfg())

        result = CleanupResult(deleted=[7, 8])
        with (
            patch(
                "blink_sync_sentry.daemon.fetch_manifest",
                new=AsyncMock(return_value=[_make_clip(7), _make_clip(8)]),
            ),
            patch(
                "blink_sync_sentry.daemon.select_clips_for_deletion",
                return_value=[_make_clip(7), _make_clip(8)],
            ),
            patch(
                "blink_sync_sentry.daemon.run_cleanup",
                new=AsyncMock(return_value=result),
            ),
        ):
            await daemon._run_cycle()

        assert daemon._pending == {"home/test": [7, 8]}

    async def test_verifies_previous_cycle_before_cleaning(self) -> None:
        sync = MagicMock()
        sync.name = "test"
        acct = MagicMock()
        acct.name = "home"
        daemon = WatchDaemon([(MagicMock(), sync, acct)], _make_cfg())
        daemon._pending["home/test"] = [7]

        result = CleanupResult()
        with (
            patch(
                "blink_sync_sentry.daemon.fetch_manifest",
                new=AsyncMock(return_value=[_make_clip(7)]),
            ),
            patch(
                "blink_sync_sentry.daemon.select_clips_for_deletion",
                return_value=[],
            ),
            patch(
                "blink_sync_sentry.daemon.run_cleanup",
                new=AsyncMock(return_value=result),
            ) as cleanup,
        ):
            await daemon._run_cycle()

        cleanup.assert_not_called()
        # Verification raised the alarm and cleared the pending set.
        assert daemon._pending == {}
