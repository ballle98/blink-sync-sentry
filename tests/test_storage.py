"""Tests for storage logic: retention filtering, age/size calculations."""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from blink_sync_sentry.storage import (
    ClipStats,
    StorageStatus,
    compute_stats,
    fetch_manifest,
    fetch_storage_status,
    filter_clips_by_age,
    filter_clips_by_usage,
    request_purge_local_storage,
    select_clips_for_deletion,
    verify_deletions,
)


def _make_clip(
    clip_id: int,
    created_at: datetime.datetime,
    size: int = 1_000_000,
    name: str = "FrontDoor",
) -> MagicMock:
    """Create a mock LocalStorageMediaItem."""
    clip = MagicMock()
    clip.id = clip_id
    clip.name = name
    clip.created_at = created_at
    clip.size = size
    return clip


NOW = datetime.datetime(2025, 6, 15, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _days_ago(n: int) -> datetime.datetime:
    return NOW - datetime.timedelta(days=n)


class TestComputeStats:
    def test_empty(self) -> None:
        stats = compute_stats([])
        assert stats.count == 0
        assert stats.total_bytes == 0
        assert stats.oldest is None
        assert stats.newest is None

    def test_single_clip(self) -> None:
        clip = _make_clip(1, _days_ago(3), size=5_000_000)
        stats = compute_stats([clip])
        assert stats.count == 1
        assert stats.total_bytes == 5_000_000
        assert stats.oldest == clip.created_at
        assert stats.newest == clip.created_at

    def test_multiple_clips(self) -> None:
        clips = [
            _make_clip(1, _days_ago(10), size=1_000_000),
            _make_clip(2, _days_ago(5), size=2_000_000),
            _make_clip(3, _days_ago(1), size=3_000_000),
        ]
        stats = compute_stats(clips)
        assert stats.count == 3
        assert stats.total_bytes == 6_000_000
        assert stats.oldest == clips[0].created_at
        assert stats.newest == clips[2].created_at

    def test_as_dict(self) -> None:
        clip = _make_clip(1, _days_ago(1), size=1_073_741_824)  # 1 GB
        stats = compute_stats([clip])
        d = stats.as_dict()
        assert d["count"] == 1
        assert d["total_gb"] == pytest.approx(1.0, rel=1e-3)


class TestFilterByAge:
    def test_no_expired(self) -> None:
        clips = [_make_clip(1, _days_ago(3)), _make_clip(2, _days_ago(1))]
        result = filter_clips_by_age(clips, retention_days=7, now=NOW)
        assert result == []

    def test_some_expired(self) -> None:
        clips = [
            _make_clip(1, _days_ago(10)),
            _make_clip(2, _days_ago(5)),
            _make_clip(3, _days_ago(1)),
        ]
        result = filter_clips_by_age(clips, retention_days=7, now=NOW)
        assert len(result) == 1
        assert result[0].id == 1

    def test_all_expired(self) -> None:
        clips = [_make_clip(1, _days_ago(30)), _make_clip(2, _days_ago(20))]
        result = filter_clips_by_age(clips, retention_days=7, now=NOW)
        assert len(result) == 2

    def test_exact_boundary_not_expired(self) -> None:
        clips = [_make_clip(1, _days_ago(7))]
        result = filter_clips_by_age(clips, retention_days=7, now=NOW)
        # Clip created exactly at cutoff is NOT expired (< cutoff, not <=).
        assert len(result) == 0


class TestFilterByUsage:
    def test_under_limit(self) -> None:
        clips = [_make_clip(1, _days_ago(3), size=500_000_000)]  # 500 MB
        result = filter_clips_by_usage(clips, max_usage_gb=1.0)
        assert result == []

    def test_over_limit_deletes_oldest(self) -> None:
        clips = [
            _make_clip(1, _days_ago(10), size=500_000_000),  # oldest
            _make_clip(2, _days_ago(5), size=500_000_000),
            _make_clip(3, _days_ago(1), size=500_000_000),   # newest
        ]
        # Total = 1.5 GB, limit = 1.0 GB → need to delete ~500 MB (oldest)
        result = filter_clips_by_usage(clips, max_usage_gb=1.0)
        assert len(result) == 1
        assert result[0].id == 1

    def test_deletes_multiple_oldest(self) -> None:
        clips = [
            _make_clip(i, _days_ago(10 - i), size=400_000_000)
            for i in range(5)
        ]
        # Total = 2 GB, limit = 0.8 GB → keep only 2 clips
        result = filter_clips_by_usage(clips, max_usage_gb=0.8)
        assert len(result) == 3

    def test_exact_limit(self) -> None:
        gb = 1024 * 1024 * 1024
        clips = [_make_clip(1, _days_ago(1), size=gb)]
        result = filter_clips_by_usage(clips, max_usage_gb=1.0)
        assert result == []


class TestSelectClipsForDeletion:
    def test_no_policy_returns_empty(self) -> None:
        clips = [_make_clip(1, _days_ago(30))]
        result = select_clips_for_deletion(clips, now=NOW)
        assert result == []

    def test_age_only(self) -> None:
        clips = [
            _make_clip(1, _days_ago(10)),
            _make_clip(2, _days_ago(1)),
        ]
        result = select_clips_for_deletion(clips, retention_days=7, now=NOW)
        assert len(result) == 1
        assert result[0].id == 1

    def test_usage_only(self) -> None:
        clips = [
            _make_clip(1, _days_ago(10), size=600_000_000),
            _make_clip(2, _days_ago(1), size=600_000_000),
        ]
        result = select_clips_for_deletion(clips, max_usage_gb=0.6, now=NOW)
        assert len(result) == 1
        assert result[0].id == 1

    def test_union_of_both_policies(self) -> None:
        clips = [
            _make_clip(1, _days_ago(30), size=100_000_000),  # age-expired
            _make_clip(2, _days_ago(5), size=600_000_000),   # usage-expired
            _make_clip(3, _days_ago(1), size=200_000_000),   # kept
        ]
        # retention_days=7 catches clip 1
        # max_usage_gb=0.3 (300 MB) catches clip 2 (and clip 1)
        result = select_clips_for_deletion(
            clips, retention_days=7, max_usage_gb=0.3, now=NOW
        )
        ids = {c.id for c in result}
        assert 1 in ids
        assert 2 in ids
        assert 3 not in ids


def _make_sync(attrs: list[str] | None = None) -> MagicMock:
    """Create a mock sync module, optionally restricted to given attributes."""
    sync = MagicMock(spec=attrs or ["name", "local_storage", "network_id", "sync_id", "blink"])
    sync.name = "test"
    sync.local_storage = True
    sync.network_id = 654321
    sync.sync_id = 123456
    sync.blink.account_id = 111
    sync.blink.urls.base_url = "https://rest.example"
    return sync


SAMPLE_STATUS = {
    "local_storage_enabled": True,
    "usb_state": "active",
    "usb_storage_used": 97,
    "usb_storage_full": True,
    "storage_warning": 0,
    "sm_backup_enabled": True,
    "last_backup_completed": "2026-11-23T06:38:07+00:00",
    "sync_module_id": 123456,
    "network_id": 654321,
    "usb_format_compatible": True,
    "sm_backup_in_progress": False,
    "last_backup_result": "success",
}


class TestStorageStatus:
    def test_from_payload_maps_fields(self) -> None:
        status = StorageStatus.from_payload(SAMPLE_STATUS)
        assert status.used_percent == 97
        assert status.full is True
        assert status.usb_state == "active"
        assert status.backup_enabled is True
        assert status.backup_in_progress is False
        assert status.last_backup_result == "success"
        assert status.raw == SAMPLE_STATUS

    def test_is_nearly_full_with_full_flag(self) -> None:
        assert StorageStatus.from_payload({"usb_storage_full": True}).is_nearly_full

    @pytest.mark.parametrize("used,expected", [(89, False), (90, True), (100, True)])
    def test_is_nearly_full_threshold(self, used: int, expected: bool) -> None:
        status = StorageStatus.from_payload({"usb_storage_used": used})
        assert status.is_nearly_full is expected

    def test_is_nearly_full_without_usage(self) -> None:
        assert not StorageStatus.from_payload({}).is_nearly_full

    def test_as_dict_round_trips_payload(self) -> None:
        assert StorageStatus.from_payload(SAMPLE_STATUS).as_dict()["used_percent"] == 97


class TestFetchStorageStatus:
    async def test_inactive_storage_returns_none(self) -> None:
        sync = _make_sync()
        sync.local_storage = False
        with patch("blink_sync_sentry.storage.api.http_get", new=AsyncMock()) as get:
            assert await fetch_storage_status(sync) is None
        get.assert_not_called()

    async def test_falls_back_to_direct_request(self) -> None:
        sync = _make_sync()
        with patch(
            "blink_sync_sentry.storage.api.http_get",
            new=AsyncMock(return_value=SAMPLE_STATUS),
        ) as get:
            status = await fetch_storage_status(sync)

        assert status is not None
        assert status.used_percent == 97
        url = get.await_args.args[1]
        assert url == (
            "https://rest.example/api/v1/accounts/111/networks/654321"
            "/sync_modules/123456/local_storage/status"
        )

    async def test_missing_endpoint_returns_none(self) -> None:
        sync = _make_sync()
        with patch(
            "blink_sync_sentry.storage.api.http_get", new=AsyncMock(return_value=None)
        ):
            assert await fetch_storage_status(sync) is None

    async def test_prefers_blinkpy_wrapper_when_present(self) -> None:
        sync = _make_sync(
            ["name", "local_storage", "network_id", "sync_id", "blink",
             "update_local_storage_status"]
        )
        sync.update_local_storage_status = AsyncMock(return_value=SAMPLE_STATUS)
        with patch("blink_sync_sentry.storage.api.http_get", new=AsyncMock()) as get:
            status = await fetch_storage_status(sync)

        assert status is not None
        assert status.full is True
        get.assert_not_called()


class TestVerifyDeletions:
    async def test_reports_ids_still_in_manifest(self) -> None:
        sync = _make_sync()
        sync.update_local_storage_manifest = AsyncMock(return_value=True)
        sync._local_storage = {"manifest": [_make_clip(2, NOW), _make_clip(3, NOW)]}

        still = await verify_deletions(sync, [1, 2])
        assert still == [2]

    async def test_all_deleted(self) -> None:
        sync = _make_sync()
        sync.update_local_storage_manifest = AsyncMock(return_value=True)
        sync._local_storage = {"manifest": []}

        assert await verify_deletions(sync, [1, 2]) == []

    async def test_skips_when_manifest_refresh_fails(self) -> None:
        sync = _make_sync()
        sync.update_local_storage_manifest = AsyncMock(return_value=None)
        sync._local_storage = {"manifest": [_make_clip(1, NOW)]}

        # Unverifiable must not be reported as a failed deletion.
        assert await verify_deletions(sync, [1]) == []

    async def test_no_ids_makes_no_request(self) -> None:
        sync = _make_sync()
        sync.update_local_storage_manifest = AsyncMock()
        assert await verify_deletions(sync, []) == []
        sync.update_local_storage_manifest.assert_not_called()


class TestFetchManifest:
    async def test_returns_clips_on_success(self) -> None:
        sync = _make_sync()
        sync.update_local_storage_manifest = AsyncMock(return_value=True)
        sync._local_storage = {"manifest": [_make_clip(1, NOW)]}

        clips = await fetch_manifest(sync)

        assert [c.id for c in clips] == [1]

    async def test_warns_on_failed_refresh(self, caplog) -> None:
        sync = _make_sync()
        sync.update_local_storage_manifest = AsyncMock(return_value=None)
        sync._local_storage = {"manifest": []}

        with caplog.at_level("WARNING", logger="blink_sync_sentry.storage"):
            clips = await fetch_manifest(sync)

        assert clips == []
        assert "may be stale" in caplog.text


class TestRequestPurgeLocalStorage:
    def _make_blink(self, status: int) -> MagicMock:
        blink = MagicMock()
        blink.urls.base_url = "https://rest.example"
        blink.account_id = 111
        blink.auth.header = {"Authorization": "Bearer token"}
        blink.auth.session.delete = AsyncMock(return_value=MagicMock(status=status))
        blink.auth.validate_response = AsyncMock(return_value={"id": 9})
        return blink

    async def test_uses_delete_verb_and_full_path(self) -> None:
        blink = self._make_blink(200)

        payload = await request_purge_local_storage(blink, _make_sync())

        assert payload == {"id": 9}
        url = blink.auth.session.delete.await_args.args[0]
        assert url == (
            "https://rest.example/api/v1/accounts/111/networks/654321"
            "/sync_modules/123456/local_storage"
        )
        blink.auth.validate_response.assert_awaited_once()

    async def test_404_is_treated_as_unavailable(self) -> None:
        blink = self._make_blink(404)

        assert await request_purge_local_storage(blink, _make_sync()) is None
        blink.auth.validate_response.assert_not_called()
