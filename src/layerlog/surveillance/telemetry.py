"""Poll the printer and keep a per-print telemetry timeline.

Each print gets ``archive_<id>.jsonl``, one line per poll, keyed by Bambuddy's
``current_archive_id`` so the timeline, the recording and the Notion row all
refer to the same print. Lines are written while the printer is busy, plus one
line at every state change, so the moment a print ends or fails is recorded
but a printer parked in FINISH doesn't keep growing the file.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..bambuddy import BambuddyClient, BambuddyError, PrinterStatus

log = logging.getLogger("layerlog.surveillance")

# States where nothing is happening. FINISH and FAILED are Bambu's usual
# gcode_state names, not yet seen from this printer. Unknown states are logged.
PARKED_STATES = {"IDLE", "FINISH", "FAILED"}


def is_active(status: PrinterStatus) -> bool:
    return status.archive_id is not None and status.state not in PARKED_STATES


def record(status: PrinterStatus, at: datetime) -> dict[str, Any]:
    d = dataclasses.asdict(status)
    del d["raw"]
    return {"ts": at.isoformat(), **d}


class TelemetryLog:
    def __init__(self, directory: Path | str):
        self.dir = Path(directory)
        self._last_state: str | None = None

    def path_for(self, archive_id: int) -> Path:
        return self.dir / f"archive_{archive_id}.jsonl"

    def observe(self, status: PrinterStatus, at: datetime) -> Path | None:
        """Append ``status`` if it belongs in a timeline; return the file written, if any."""
        changed = status.state != self._last_state
        self._last_state = status.state
        if status.archive_id is None or (status.state in PARKED_STATES and not changed):
            return None
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.path_for(status.archive_id)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record(status, at), ensure_ascii=False) + "\n")
        return path


def run(client: BambuddyClient, printer_id: int, tlog: TelemetryLog, interval: float, *,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], None] = time.sleep,
        polls: int | None = None,
        on_status: Callable[[PrinterStatus], None] | None = None) -> None:
    """Poll forever (or ``polls`` times). A failed poll leaves a gap in the timeline.

    ``on_status`` sees every successful poll (the recorder hangs off this).
    """
    n = 0
    last_state: str | None = None
    while polls is None or n < polls:
        n += 1
        started = time.monotonic()
        try:
            status = client.printer_status(printer_id)
        except BambuddyError as e:
            log.warning("poll failed: %s", e)
        else:
            path = tlog.observe(status, now())
            if on_status:
                on_status(status)
            if status.state != last_state:
                log.info("state %s, archive %s, layer %s/%s -> %s", status.state, status.archive_id,
                         status.layer, status.total_layers, path.name if path else "not logged")
                last_state = status.state
        sleep(max(0.0, interval - (time.monotonic() - started)))
