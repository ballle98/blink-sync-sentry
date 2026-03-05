"""Tests for storage logic: retention filtering, age/size calculations."""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock

import pytest

from blink_sync_sentry.storage import (
    ClipStats,
    compute_stats,
    filter_clips_by_age,
    filter_clips_by_usage,
    select_clips_for_deletion,
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
