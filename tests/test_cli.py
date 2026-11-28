"""Tests for CLI cleanup orchestration and exit codes."""

from __future__ import annotations

import argparse
from unittest.mock import AsyncMock, MagicMock, patch

from blink_sync_sentry.cleanup import CleanupResult, PurgeResult
from blink_sync_sentry.cli import EXIT_DRY_RUN, EXIT_ERROR, EXIT_OK, _cmd_clean


def _make_clip(clip_id: int) -> MagicMock:
    clip = MagicMock()
    clip.id = clip_id
    return clip


def _make_cfg() -> MagicMock:
    cfg = MagicMock()
    cfg.request_delay_seconds = 0
    cfg.effective_retention.return_value = MagicMock(
        retention_days=1, max_usage_gb=None
    )
    cfg.effective_archive_dir.return_value = None
    return cfg


def _make_args(*, force: bool) -> argparse.Namespace:
    return argparse.Namespace(
        force=force,
        use_json=False,
        sync_module_name="test",
        retention_days=1,
        max_usage_gb=None,
        archive_dir=None,
    )


def _make_sync() -> MagicMock:
    sync = MagicMock()
    sync.name = "test"
    sync.local_storage = True
    return sync


async def _run_clean(force: bool, *, deleted: list[int], still_present: list[int]) -> int:
    acct = MagicMock()
    acct.name = "home"
    sync = _make_sync()
    blink = MagicMock()
    blink.sync = {"test": sync}

    with (
        patch("blink_sync_sentry.cli.create_blink", new=AsyncMock(return_value=blink)),
        patch("blink_sync_sentry.cli.close_blink", new=AsyncMock()),
        patch(
            "blink_sync_sentry.cli.fetch_manifest",
            new=AsyncMock(return_value=[_make_clip(1)]),
        ),
        patch("blink_sync_sentry.cli.fetch_storage_status", new=AsyncMock(return_value=None)),
        patch(
            "blink_sync_sentry.cli.select_clips_for_deletion",
            return_value=[_make_clip(1)],
        ),
        patch(
            "blink_sync_sentry.cli.run_cleanup",
            new=AsyncMock(return_value=CleanupResult(deleted=deleted)),
        ) as cleanup,
        patch(
            "blink_sync_sentry.cli.verify_deletions",
            new=AsyncMock(return_value=still_present),
        ) as verify,
    ):
        code = await _cmd_clean(_make_cfg(), _make_args(force=force), [acct])

    if not force:
        cleanup.assert_not_called()
        verify.assert_not_called()
    return code


class TestCleanExitCodes:
    async def test_dry_run_exits_two_without_deleting(self) -> None:
        assert await _run_clean(False, deleted=[], still_present=[]) == EXIT_DRY_RUN

    async def test_verified_deletions_exit_zero(self) -> None:
        assert await _run_clean(True, deleted=[1], still_present=[]) == EXIT_OK

    async def test_clips_still_present_exit_one(self) -> None:
        assert await _run_clean(True, deleted=[1], still_present=[1]) == EXIT_ERROR


async def _run_purge(force: bool, *, verified: bool) -> tuple[int, AsyncMock]:
    acct = MagicMock()
    acct.name = "home"
    sync = _make_sync()
    blink = MagicMock()
    blink.sync = {"test": sync}
    cfg = _make_cfg()
    # A purge does not need a retention policy configured.
    cfg.effective_retention.return_value = MagicMock(
        retention_days=None, max_usage_gb=None
    )

    args = _make_args(force=force)
    args.purge_all = True
    purge = PurgeResult(
        skipped_dry_run=not force, verified=verified, command_accepted=force
    )
    with (
        patch("blink_sync_sentry.cli.create_blink", new=AsyncMock(return_value=blink)),
        patch("blink_sync_sentry.cli.close_blink", new=AsyncMock()),
        patch("blink_sync_sentry.cli.format_purge_result"),
        patch(
            "blink_sync_sentry.cli.fetch_manifest", new=AsyncMock(return_value=[])
        ) as manifest,
        patch(
            "blink_sync_sentry.cli.fetch_storage_status", new=AsyncMock(return_value=None)
        ),
        patch(
            "blink_sync_sentry.cli.purge_all_clips",
            new=AsyncMock(return_value=purge),
        ) as purger,
    ):
        code = await _cmd_clean(cfg, args, [acct])

    if not force:
        manifest.assert_not_awaited()
    return code, purger


class TestPurgeExitCodes:
    async def test_dry_run_exits_two_without_purging(self) -> None:
        code, purger = await _run_purge(False, verified=False)

        assert code == EXIT_DRY_RUN
        purger.assert_not_awaited()

    async def test_verified_purge_exits_zero(self) -> None:
        code, purger = await _run_purge(True, verified=True)

        assert code == EXIT_OK
        purger.assert_awaited_once()
        assert purger.await_args.kwargs["force"] is True

    async def test_unverified_purge_exits_one(self) -> None:
        code, _ = await _run_purge(True, verified=False)

        assert code == EXIT_ERROR
