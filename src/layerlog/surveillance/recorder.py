"""Record the camera stream while a print runs.

ffmpeg copies the MJPEG stream (no re-encode) into Matroska segments under
``<dir>/archive_<id>/``. ``-use_wallclock_as_timestamps`` with ``-copyts``
makes every frame's timestamp its arrival time in Unix seconds, so frames line
up with the telemetry timeline exactly, even across restarts.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("layerlog.surveillance")

RESTART_BACKOFF_S = 10.0


class Recorder:
    def __init__(self, stream_url: str, directory: Path | str, *, segment_seconds: int = 3600,
                 ffmpeg: str = "ffmpeg", popen: Callable[..., subprocess.Popen] = subprocess.Popen,
                 clock: Callable[[], float] = time.monotonic):
        self.stream_url = stream_url
        self.dir = Path(directory)
        self.segment_seconds = segment_seconds
        self.ffmpeg = ffmpeg
        self._popen = popen
        self._clock = clock
        self._proc: subprocess.Popen | None = None
        self._archive_id: int | None = None
        self._started_at = float("-inf")

    def segment_dir(self, archive_id: int) -> Path:
        return self.dir / f"archive_{archive_id}"

    def command(self, archive_id: int) -> list[str]:
        return [
            self.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning",
            "-use_wallclock_as_timestamps", "1", "-f", "mpjpeg", "-i", self.stream_url,
            "-c:v", "copy", "-copyts",
            "-f", "segment", "-segment_time", str(self.segment_seconds),
            "-segment_format", "matroska", "-strftime", "1",
            str(self.segment_dir(archive_id) / "%Y%m%dT%H%M%SZ.mkv"),
        ]

    @property
    def recording(self) -> int | None:
        """The archive being recorded, if ffmpeg is running."""
        return self._archive_id if self._proc and self._proc.poll() is None else None

    def ensure(self, archive_id: int | None) -> None:
        """Record ``archive_id`` (None stops). Called every poll; restarts a dead ffmpeg."""
        if archive_id is None:
            self.stop()
            return
        if self._archive_id != archive_id:
            self.stop()
        elif self._proc:
            code = self._proc.poll()
            if code is None:
                return
            log.warning("ffmpeg exited with %s while recording archive %s", code, archive_id)
            self._proc = None
        if self._clock() - self._started_at < RESTART_BACKOFF_S:
            return
        self.segment_dir(archive_id).mkdir(parents=True, exist_ok=True)
        # TZ=UTC so -strftime names segments in UTC, like the telemetry timestamps.
        self._proc = self._popen(self.command(archive_id), stdin=subprocess.DEVNULL,
                                 env={**os.environ, "TZ": "UTC"}, start_new_session=True)
        self._archive_id = archive_id
        self._started_at = self._clock()
        log.info("recording archive %s -> %s", archive_id, self.segment_dir(archive_id))

    def stop(self) -> None:
        proc, self._proc, self._archive_id = self._proc, None, None
        if proc is None or proc.poll() is not None:
            return
        proc.send_signal(signal.SIGINT)  # lets ffmpeg finish the open segment cleanly
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log.info("recording stopped")
