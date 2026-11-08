"""``python -m layerlog.surveillance``: run the telemetry poller, camera recorder and renderer.

Environment: BAMBUDDY_URL (required), BAMBUDDY_API_KEY (only when Bambuddy auth
is on), PRINTER_ID (default 1), TELEMETRY_DIR (default ./telemetry),
POLL_SECONDS (default 2), RECORDINGS_DIR (default ./recordings),
RECORD (default 1; 0 disables the recorder and renderer), RENDERS_DIR
(default ./renders), RENDER_EVERY_SECONDS (default 3600).

On start, raw recordings of prints that ended while this wasn't running get
their final render (see ``recover_missed_ends``).
"""

import logging
import os
import shutil
import signal
import sys
from pathlib import Path

from ..bambuddy import BambuddyClient, PrinterStatus
from . import render
from .autorender import RenderScheduler, RenderWorker, recover_missed_ends
from .recorder import Recorder
from .telemetry import TelemetryLog, is_active, run

log = logging.getLogger("layerlog.surveillance")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # docker stop sends SIGTERM; exit through the finally below so ffmpeg is stopped too.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    printer_id = int(os.environ.get("PRINTER_ID", "1"))
    telemetry_dir = Path(os.environ.get("TELEMETRY_DIR", "telemetry"))
    recordings_dir = Path(os.environ.get("RECORDINGS_DIR", "recordings"))
    renders_dir = Path(os.environ.get("RENDERS_DIR", "renders"))
    with BambuddyClient(os.environ["BAMBUDDY_URL"], os.environ.get("BAMBUDDY_API_KEY") or None,
                        timeout=10.0) as client:
        recorder = scheduler = None
        if os.environ.get("RECORD", "1") != "0":
            if shutil.which("ffmpeg"):
                recorder = Recorder(client.camera_stream_url(printer_id), recordings_dir)
                worker = RenderWorker(lambda a, final: render.render(a, telemetry_dir, recordings_dir,
                                                                       renders_dir, final=final))
                scheduler = RenderScheduler(worker.request,
                                            interval=float(os.environ.get("RENDER_EVERY_SECONDS", "3600")))
                recover_missed_ends(client, printer_id, recordings_dir, worker.request)
            else:
                log.warning("ffmpeg not found; recording disabled")

        def on_status(s: PrinterStatus) -> None:
            if recorder:
                recorder.ensure(s.archive_id if is_active(s) else None)  # stops before the final render
            if scheduler:
                scheduler.observe(s)

        try:
            run(client, printer_id, TelemetryLog(telemetry_dir),
                float(os.environ.get("POLL_SECONDS", "2")), on_status=on_status)
        except KeyboardInterrupt:
            pass
        finally:
            if recorder:
                recorder.stop()


if __name__ == "__main__":
    main()
