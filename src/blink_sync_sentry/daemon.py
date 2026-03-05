"""Watch-mode daemon: periodic clean cycle with graceful shutdown.

Supports watching multiple sync modules across multiple Blink accounts.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import TYPE_CHECKING, Sequence

from blink_sync_sentry.cleanup import run_cleanup
from blink_sync_sentry.storage import fetch_manifest, select_clips_for_deletion

if TYPE_CHECKING:
    from blinkpy.blinkpy import Blink
    from blinkpy.sync_module import BlinkSyncModule

    from blink_sync_sentry.config import AccountConfig, AppConfig

_LOGGER = logging.getLogger(__name__)


class WatchDaemon:
    """Runs cleanup on a fixed interval until stopped.

    *targets* is a list of ``(Blink, BlinkSyncModule, AccountConfig)`` tuples
    representing every sync module that should be monitored.
    """

    def __init__(
        self,
        targets: Sequence[tuple[Blink, BlinkSyncModule, AccountConfig]],
        cfg: AppConfig,
    ) -> None:
        self.targets = targets
        self.cfg = cfg
        self._stop_event = asyncio.Event()

    async def run(self) -> None:
        """Start the watch loop. Blocks until a stop signal is received."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._request_stop)

        interval_sec = self.cfg.watch.interval_minutes * 60
        _LOGGER.info(
            "Watch daemon started — monitoring %d sync module(s) across %d account(s). "
            "Interval: %d min. Ctrl-C or SIGTERM to stop.",
            len(self.targets),
            len({a.name for _, _, a in self.targets}),
            self.cfg.watch.interval_minutes,
        )

        while not self._stop_event.is_set():
            await self._run_cycle()
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=interval_sec
                )
            except asyncio.TimeoutError:
                pass  # Normal: timeout means it's time for the next cycle.

        _LOGGER.info("Watch daemon shutting down gracefully.")

    async def _run_cycle(self) -> None:
        """Execute one clean cycle across all targets."""
        _LOGGER.info("=== Watch cycle starting ===")
        for blink, sync, acct in self.targets:
            label = f"{acct.name}/{sync.name}"
            try:
                retention = self.cfg.effective_retention(acct)
                archive_dir = self.cfg.effective_archive_dir(acct)

                clips = await fetch_manifest(sync)
                to_delete = select_clips_for_deletion(
                    clips,
                    retention_days=retention.retention_days,
                    max_usage_gb=retention.max_usage_gb,
                )
                if not to_delete:
                    _LOGGER.info("[%s] Nothing to clean.", label)
                    continue

                result = await run_cleanup(
                    blink,
                    to_delete,
                    force=True,
                    archive_dir=archive_dir,
                    request_delay=self.cfg.request_delay_seconds,
                )
                _LOGGER.info(
                    "[%s] Cycle result: %d deleted, %d failed.",
                    label,
                    len(result.deleted),
                    len(result.failed),
                )
            except Exception:
                _LOGGER.exception(
                    "[%s] Error during watch cycle — will retry next interval.",
                    label,
                )
        _LOGGER.info("=== Watch cycle complete ===")

    def _request_stop(self) -> None:
        _LOGGER.info("Stop signal received.")
        self._stop_event.set()
