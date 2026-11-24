"""Tests for output formatting."""

from __future__ import annotations

import json

from blink_sync_sentry.output import format_clip_stats
from blink_sync_sentry.storage import ClipStats, StorageStatus

EMPTY_STATS = ClipStats(count=0, total_bytes=0, oldest=None, newest=None)


def _status(**overrides) -> StorageStatus:
    payload = {
        "usb_storage_used": 97,
        "usb_storage_full": True,
        "sm_backup_enabled": True,
        "last_backup_result": "success",
    }
    payload.update(overrides)
    return StorageStatus.from_payload(payload)


class TestFormatClipStatsStorage:
    def test_reports_measured_usage(self, capsys) -> None:
        format_clip_stats("Module", EMPTY_STATS, storage_status=_status())
        out = capsys.readouterr().out

        assert "97% used (reported by sync module, flagged full)" in out

    def test_labels_capacity_estimate_when_status_present(self, capsys) -> None:
        format_clip_stats(
            "Module", EMPTY_STATS, storage_capacity_gb=32, storage_status=_status()
        )
        out = capsys.readouterr().out

        assert "32GB drive (manifest estimate)" in out

    def test_no_status_keeps_previous_output(self, capsys) -> None:
        format_clip_stats("Module", EMPTY_STATS)
        out = capsys.readouterr().out

        assert "Drive:" not in out
        assert "Clips:      0" in out

    def test_json_includes_drive_block(self, capsys) -> None:
        format_clip_stats("Module", EMPTY_STATS, use_json=True, storage_status=_status())
        data = json.loads(capsys.readouterr().out)

        assert data["drive"]["used_percent"] == 97
        assert data["drive"]["full"] is True
        assert data["drive"]["last_backup_result"] == "success"

    def test_json_without_status_has_no_drive_block(self, capsys) -> None:
        format_clip_stats("Module", EMPTY_STATS, use_json=True)
        data = json.loads(capsys.readouterr().out)

        assert "drive" not in data
