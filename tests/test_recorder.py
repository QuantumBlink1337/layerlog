import signal

from layerlog.bambuddy import BambuddyClient
from layerlog.surveillance.recorder import Recorder


class FakeProc:
    def __init__(self, cmd, **kw):
        self.cmd, self.kw, self.code, self.signals = cmd, kw, None, []

    def poll(self):
        return self.code

    def send_signal(self, sig):
        self.signals.append(sig)
        self.code = 255

    def wait(self, timeout=None):
        return self.code


def make(tmp_path):
    t = [0.0]
    procs: list[FakeProc] = []

    def popen(cmd, **kw):
        procs.append(FakeProc(cmd, **kw))
        return procs[-1]

    rec = Recorder("http://bambuddy:8000/api/v1/printers/1/camera/stream", tmp_path,
                   popen=popen, clock=lambda: t[0])
    return rec, procs, t


def test_command_copies_stream_with_wallclock_timestamps(tmp_path):
    cmd = Recorder("http://cam", tmp_path).command(27)
    assert cmd[cmd.index("-i") + 1] == "http://cam"
    assert {"-copyts", "-nostdin"} <= set(cmd)
    assert cmd[cmd.index("-use_wallclock_as_timestamps") + 1] == "1"
    assert cmd[cmd.index("-c:v") + 1] == "copy"
    assert cmd[-1] == str(tmp_path / "archive_27" / "%Y%m%dT%H%M%SZ.mkv")


def test_starts_once_and_names_segments_in_utc(tmp_path):
    rec, procs, _ = make(tmp_path)
    rec.ensure(27)
    rec.ensure(27)
    assert len(procs) == 1 and rec.recording == 27
    assert procs[0].kw["env"]["TZ"] == "UTC" and (tmp_path / "archive_27").is_dir()


def test_dead_ffmpeg_restarts_after_backoff(tmp_path):
    rec, procs, t = make(tmp_path)
    rec.ensure(27)
    procs[0].code = 1
    t[0] = 5
    rec.ensure(27)
    assert len(procs) == 1 and rec.recording is None  # still backing off
    t[0] = 11
    rec.ensure(27)
    assert len(procs) == 2 and rec.recording == 27


def test_none_stops_with_sigint_and_new_archive_switches(tmp_path):
    rec, procs, t = make(tmp_path)
    rec.ensure(27)
    t[0] = 100
    rec.ensure(28)
    assert procs[0].signals == [signal.SIGINT] and rec.recording == 28
    rec.ensure(None)
    assert procs[1].signals == [signal.SIGINT] and rec.recording is None


def test_camera_stream_url():
    with BambuddyClient("http://192.168.0.39:8000/") as c:
        assert c.camera_stream_url(1) == "http://192.168.0.39:8000/api/v1/printers/1/camera/stream"


# --- real ffmpeg: the exact recorder command against a fake Bambuddy camera stream ---

import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from layerlog.surveillance.render import segment_span

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def jpeg_frames(n: int) -> list[bytes]:
    out = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10",
                          "-frames:v", str(n), "-c:v", "mjpeg", "-f", "image2pipe", "-"],
                         check=True, capture_output=True).stdout
    frames, start = [], 0
    while (end := out.find(b"\xff\xd9", start)) != -1:  # JPEG end-of-image marker
        frames.append(out[start:end + 2])
        start = end + 2
    return frames


def mjpeg_server(frames: list[bytes]) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # like Bambuddy: multipart/x-mixed-replace; boundary=frame, ~10 fps
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                for f in frames:
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                     b"Content-Length: %d\r\n\r\n" % len(f) + f + b"\r\n")
                    self.wfile.flush()
                    time.sleep(0.1)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@needs_ffmpeg
def test_real_ffmpeg_records_mjpeg_stream_with_unix_timestamps(tmp_path):
    srv = mjpeg_server(jpeg_frames(40))
    try:
        rec = Recorder(f"http://127.0.0.1:{srv.server_port}/stream", tmp_path)
        before = time.time()
        rec.ensure(27)
        time.sleep(3)
        rec.stop()
    finally:
        srv.shutdown()
    segments = sorted((tmp_path / "archive_27").glob("*.mkv"))
    assert len(segments) == 1
    # The segment is named in UTC (-strftime with TZ=UTC) ...
    named = time.mktime(time.strptime(segments[0].stem, "%Y%m%dT%H%M%SZ")) - time.timezone
    assert abs(named - before) < 5
    # ... and -use_wallclock_as_timestamps + -copyts make its pts Unix arrival times.
    t0, t1 = segment_span(segments[0])
    assert abs(t0 - before) < 5
    assert 1 < t1 - t0 < 5
