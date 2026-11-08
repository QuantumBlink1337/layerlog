"""``python -m layerlog.surveillance``: run the telemetry poller and camera recorder.

Environment: BAMBUDDY_URL (required), BAMBUDDY_API_KEY (only when Bambuddy auth
is on), PRINTER_ID (default 1), TELEMETRY_DIR (default ./telemetry),
POLL_SECONDS (default 2), RECORDINGS_DIR (default ./recordings),
RECORD (default 1; 0 disables the recorder).
"""

import logging
import os
import shutil

from ..bambuddy import BambuddyClient
from .recorder import Recorder
from .telemetry import TelemetryLog, is_active, run

log = logging.getLogger("layerlog.surveillance")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    printer_id = int(os.environ.get("PRINTER_ID", "1"))
    with BambuddyClient(os.environ["BAMBUDDY_URL"], os.environ.get("BAMBUDDY_API_KEY") or None,
                        timeout=10.0) as client:
        recorder = None
        if os.environ.get("RECORD", "1") != "0":
            if shutil.which("ffmpeg"):
                recorder = Recorder(client.camera_stream_url(printer_id),
                                    os.environ.get("RECORDINGS_DIR", "recordings"))
            else:
                log.warning("ffmpeg not found; recording disabled")
        on_status = (lambda s: recorder.ensure(s.archive_id if is_active(s) else None)) if recorder else None
        try:
            run(client, printer_id, TelemetryLog(os.environ.get("TELEMETRY_DIR", "telemetry")),
                float(os.environ.get("POLL_SECONDS", "2")), on_status=on_status)
        except KeyboardInterrupt:
            pass
        finally:
            if recorder:
                recorder.stop()


if __name__ == "__main__":
    main()
