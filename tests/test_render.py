import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from layerlog.bambuddy import PrinterStatus
from layerlog.surveillance import render
from layerlog.surveillance.telemetry import record

FIXTURES = Path(__file__).parent / "fixtures"
STATUS = json.loads((FIXTURES / "printer_1_status_printing.json").read_text())
T0 = datetime(2026, 11, 8, 21, 0, tzinfo=timezone.utc)


def rec(seconds: float, **over) -> dict:
    return record(PrinterStatus.from_json({**STATUS, **over}), T0 + timedelta(seconds=seconds))


def test_overlay_text_from_real_poll():
    assert render.overlay_text(rec(0)) == (
        "L 553/1415   41%   18h23m left   Printing\\N"
        "Nozzle 220/220   Bed 65/65   Chamber 34   Fan P100 A47 C47")


def test_overlay_text_tolerates_missing_values():
    assert render.overlay_text({"state": "IDLE"}).startswith("L -/-   -%   -   IDLE")


def test_cues_merge_identical_polls_and_blank_long_gaps():
    timeline = [rec(0), rec(2), rec(4, layer_num=554), rec(60, layer_num=555)]
    cs = render.cues(timeline)
    t = T0.timestamp()
    assert [(c.start - t, c.end - t) for c in cs] == [(0, 4), (4, 14), (60, 70)]
    assert "L 554/" in cs[1].text


def test_build_ass_is_relative_to_segment_start_and_clipped():
    t = T0.timestamp()
    cs = [render.Cue(t, t + 4, "a"), render.Cue(t + 4, t + 3700, "b"), render.Cue(t + 3700, t + 3710, "c")]
    events = [l for l in render.build_ass(cs, t + 2, t + 3602).splitlines() if l.startswith("Dialogue")]
    assert events == ["Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,a",
                      "Dialogue: 0,0:00:02.00,1:01:38.00,Default,,0,0,0,,b"]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_render_end_to_end(tmp_path):
    seg_dir = tmp_path / "recordings" / "archive_27"
    seg_dir.mkdir(parents=True)
    # A 4 s clip whose timestamps are Unix seconds, like the recorder's -copyts output.
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10",
                    "-t", "4", "-c:v", "mjpeg", "-output_ts_offset", str(T0.timestamp()), "-copyts",
                    str(seg_dir / "20261108T210000Z.mkv")], check=True)
    (tmp_path / "telemetry").mkdir()
    (tmp_path / "telemetry" / "archive_27.jsonl").write_text(
        "".join(json.dumps(rec(s, layer_num=553 + s // 2)) + "\n" for s in (0, 2)))

    out = render.render(27, tmp_path / "telemetry", tmp_path / "recordings", tmp_path / "renders")
    assert render.segment_span(seg_dir / "20261108T210000Z.mkv")[0] == pytest.approx(T0.timestamp())
    probe = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name:format=duration", "-of", "json", str(out)],
        check=True, capture_output=True, text=True).stdout)
    assert probe["streams"][0]["codec_name"] == "h264"
    assert float(probe["format"]["duration"]) == pytest.approx(4, abs=0.2)
