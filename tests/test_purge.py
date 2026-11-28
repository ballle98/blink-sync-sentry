"""Tests for the guarded local-storage purge."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from blink_sync_sentry.cleanup import purge_all_clips


def _make_sync() -> MagicMock:
    sync = MagicMock()
    sync.name = "test"
    sync.local_storage = True
    sync.network_id = 654321
    sync.sync_id = 123456
    return sync


def _status(used: int | None) -> MagicMock | None:
    if used is None:
        return None
    status = MagicMock()
    status.used_percent = used
    return status


def _clip(clip_id: int) -> MagicMock:
    clip = MagicMock()
    clip.id = clip_id
    return clip


async def _purge(
    *,
    force: bool,
    before: int | None,
    after: int | None,
    endpoint: dict | None | object = "default",
) -> object:
    if endpoint == "default":
        endpoint = {"id": 9, "network_id": 654321}
    with (
        patch(
            "blink_sync_sentry.cleanup.fetch_storage_status",
            new=AsyncMock(side_effect=[_status(before), _status(after)]),
        ),
        patch(
            "blink_sync_sentry.cleanup.request_purge_local_storage",
            new=AsyncMock(return_value=endpoint),
        ) as request,
        patch("blink_sync_sentry.cleanup.api.wait_for_command", new=AsyncMock()),
    ):
        result = await purge_all_clips(MagicMock(), _make_sync(), force=force)
    return result, request


class TestPurgeGuard:
    async def test_dry_run_sends_nothing(self) -> None:
        with (
            patch(
                "blink_sync_sentry.cleanup.request_purge_local_storage",
                new=AsyncMock(),
            ) as request,
            patch(
                "blink_sync_sentry.cleanup.fetch_manifest", new=AsyncMock()
            ) as manifest,
        ):
            result = await purge_all_clips(MagicMock(), _make_sync(), force=False)

        assert result.skipped_dry_run is True
        assert result.verified is False
        request.assert_not_called()
        manifest.assert_not_called()

    async def test_archive_failure_aborts_before_delete(self) -> None:
        with (
            patch(
                "blink_sync_sentry.cleanup.fetch_manifest",
                new=AsyncMock(return_value=[_clip(1)]),
            ),
            patch("blink_sync_sentry.cleanup.archive_clip", new=AsyncMock(return_value=None)),
            patch(
                "blink_sync_sentry.cleanup.request_purge_local_storage",
                new=AsyncMock(),
            ) as request,
        ):
            result = await purge_all_clips(
                MagicMock(), _make_sync(), force=True, archive_dir="/tmp/archive"
            )

        assert not result.command_accepted
        assert "archiving clip 1 failed" in result.note
        request.assert_not_called()


class TestPurgeVerification:
    async def test_usage_drop_is_verified(self) -> None:
        result, request = await _purge(force=True, before=97, after=1)

        assert result.command_accepted is True
        assert result.usage_before == 97
        assert result.usage_after == 1
        assert result.verified is True
        request.assert_awaited_once()

    async def test_unchanged_usage_is_not_verified(self) -> None:
        result, _ = await _purge(force=True, before=97, after=97)

        assert result.verified is False
        assert "did not drop" in result.note

    async def test_unavailable_endpoint_is_not_verified(self) -> None:
        result, _ = await _purge(force=True, before=97, after=1, endpoint=None)

        assert result.command_accepted is False
        assert result.verified is False
        assert "unavailable" in result.note

    async def test_falls_back_to_manifest_when_usage_unavailable(self) -> None:
        with (
            patch(
                "blink_sync_sentry.cleanup.fetch_storage_status", new=AsyncMock(return_value=None)
            ),
            patch(
                "blink_sync_sentry.cleanup.request_purge_local_storage",
                new=AsyncMock(return_value={"id": 9, "network_id": 654321}),
            ),
            patch("blink_sync_sentry.cleanup.api.wait_for_command", new=AsyncMock()),
            patch(
                "blink_sync_sentry.cleanup.fetch_manifest", new=AsyncMock(return_value=[])
            ) as manifest,
        ):
            result = await purge_all_clips(MagicMock(), _make_sync(), force=True)

        assert result.verified is True
        manifest.assert_awaited_once()
