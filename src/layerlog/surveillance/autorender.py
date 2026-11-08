"""Decide when to render, and render off the poll loop.

A print is rendered every ``interval`` seconds while it runs, as soon as it
pauses (manual pause, or the printer's own error detection pausing it), and
once more when it ends. Renders run one at a time on a background thread;
requests that arrive meanwhile are coalesced.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path

from ..bambuddy import BambuddyClient, BambuddyError, PrinterStatus
from .telemetry import is_active

log = logging.getLogger("layerlog.surveillance")

# The final render deletes the raw recording, so a print only counts as ended
# once it has looked ended for this long (a status blip mid-print must not).
END_GRACE_S = 30.0


def is_paused(status: PrinterStatus) -> bool:
    # Bambu reports gcode_state PAUSE; the stage name ("Paused by the user",
    # "Pause due to ...") varies by cause, so either one counts.
    return status.state == "PAUSE" or "pause" in (status.stage or "").lower()


class RenderScheduler:
    def __init__(self, request: Callable[[int, str, bool], None], *, interval: float = 3600.0,
                 clock: Callable[[], float] = time.monotonic):
        self._request = request
        self.interval = interval
        self._clock = clock
        self._archive_id: int | None = None
        self._paused = False
        self._last = 0.0
        self._parked_since: float | None = None

    def _fire(self, archive_id: int, reason: str, final: bool = False) -> None:
        self._last = self._clock()
        self._request(archive_id, reason, final)

    def observe(self, status: PrinterStatus) -> None:
        """Call once per poll, after the recorder has reacted to the same status."""
        if is_active(status):
            self._parked_since = None
            if status.archive_id != self._archive_id:
                if self._archive_id is not None:
                    self._fire(self._archive_id, "print ended", True)
                self._archive_id, self._paused, self._last = status.archive_id, False, self._clock()
            paused = is_paused(status)
            if paused and not self._paused:
                self._fire(status.archive_id, f"paused ({status.stage or status.state})")
            elif self._clock() - self._last >= self.interval:
                self._fire(status.archive_id, "hourly")
            self._paused = paused
        elif self._archive_id is not None:
            if self._parked_since is None:
                self._parked_since = self._clock()
            elif self._clock() - self._parked_since >= END_GRACE_S:
                self._fire(self._archive_id, f"print ended ({status.state})", True)
                self._archive_id = self._parked_since = None


class RenderWorker:
    """Runs ``render(archive_id, final)`` on a daemon thread, one job at a time."""

    def __init__(self, render: Callable[[int, bool], object]):
        self._render = render
        self._pending: dict[int, tuple[str, bool]] = {}
        self._cv = threading.Condition()
        self._busy = False
        self._thread = threading.Thread(target=self._loop, name="render", daemon=True)
        self._thread.start()

    def request(self, archive_id: int, reason: str, final: bool = False) -> None:
        with self._cv:
            if archive_id in self._pending:
                old_reason, old_final = self._pending[archive_id]
                self._pending[archive_id] = (f"{old_reason}, {reason}", old_final or final)
            else:
                self._pending[archive_id] = (reason, final)
            self._cv.notify()

    def idle(self) -> bool:
        with self._cv:
            return not self._pending and not self._busy

    def _loop(self) -> None:
        while True:
            with self._cv:
                while not self._pending:
                    self._cv.wait()
                archive_id = next(iter(self._pending))
                reason, final = self._pending.pop(archive_id)
                self._busy = True
            log.info("rendering archive %s (%s)", archive_id, reason)
            try:
                log.info("rendered %s", self._render(archive_id, final))
            except Exception:
                log.exception("render of archive %s failed", archive_id)
            finally:
                with self._cv:
                    self._busy = False
                    self._cv.notify_all()


_RAW_DIR = re.compile(r"archive_(\d+)")


def recover_missed_ends(client: BambuddyClient, printer_id: int, recordings_dir: Path,
                        request: Callable[[int, str, bool], None]) -> list[int]:
    """Queue the final render for prints that ended while we weren't running.

    Without this their raw footage would stay forever. The final render deletes
    the raw recording, so an archive only qualifies if it is not the printer's
    active print *and* Bambuddy no longer reports it as ``printing``; anything
    in doubt is left alone (and retried on the next start).
    """
    if not recordings_dir.is_dir():
        return []
    try:
        status = client.printer_status(printer_id)
    except BambuddyError as e:
        log.warning("missed-end check skipped: printer status unavailable (%s)", e)
        return []
    active = status.archive_id if is_active(status) else None
    queued = []
    for d in sorted(recordings_dir.iterdir()):
        m = _RAW_DIR.fullmatch(d.name)
        if not (d.is_dir() and m) or int(m[1]) == active:
            continue
        archive_id = int(m[1])
        try:
            archive_status = client.get_archive(archive_id).status
        except BambuddyError as e:
            log.warning("leaving raw recording for archive %s: %s", archive_id, e)
            continue
        if archive_status == "printing":
            log.warning("leaving raw recording for archive %s: Bambuddy still says printing",
                        archive_id)
            continue
        request(archive_id, f"missed end ({archive_status})", True)
        queued.append(archive_id)
    return queued
