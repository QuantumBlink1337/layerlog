import json
import os
import shutil
import subprocess
import time
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


VIDEO = "2026-11-08 Apricity-Complete-chain-rod-chaingap #27.mp4"  # T0 is 15:00 in Chicago


@pytest.fixture(autouse=True)
def chicago(monkeypatch):
    monkeypatch.setenv("TZ", "America/Chicago")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_video_name_uses_local_start_date_and_print_name():
    late = [rec(6 * 3600)]  # 03:00 UTC on the 9th is still the 8th in Chicago
    assert render.video_name(27, late) == VIDEO
    named = [rec(0, current_print=None), rec(2, current_print="Beeba-baseline-support_PLA_17h29m.gcode.3mf"),
             rec(4, current_print="something else")]
    assert render.video_name(28, named) == "2026-11-08 Beeba-baseline-support_PLA #28.mp4"
    assert render.video_name(29, [rec(0, current_print='a/b:c?')]) == "2026-11-08 a_b_c_ #29.mp4"


def test_video_name_falls_back_to_archive_id():
    assert render.video_name(27, []) == "archive_27.mp4"
    assert render.video_name(27, [rec(0, current_print="")]) == "archive_27.mp4"


def test_rename_legacy(tmp_path):
    (tmp_path / "telemetry").mkdir()
    (tmp_path / "telemetry" / "archive_27.jsonl").write_text(json.dumps(rec(0)) + "\n")
    renders = tmp_path / "renders"
    renders.mkdir()
    (renders / "archive_27.mp4").write_bytes(b"video")
    assert render.rename_legacy(27, tmp_path / "telemetry", renders) == renders / VIDEO
    assert [p.name for p in renders.iterdir()] == [VIDEO]
    assert render.rename_legacy(27, tmp_path / "telemetry", renders) is None  # already done
    (renders / "archive_28.mp4").write_bytes(b"video")
    assert render.rename_legacy(28, tmp_path / "telemetry", renders) is None  # no telemetry, no name


def test_overlay_text_from_real_poll():
    assert render.overlay_text(rec(0)) == (
        "L 553/1415   41%   18h23m left   Printing\\N"
        "Nozzle 220/220   Bed 65/65   Chamber 34   Fan P100 A47 C47")


def test_overlay_text_shows_non_running_state_even_if_stage_says_printing():
    assert "PAUSE (Printing)" in render.overlay_text(rec(0, state="PAUSE"))
    assert render.overlay_text(rec(0, state="FINISH", stg_cur_name=None)).split("\\N")[0].endswith("FINISH")


def test_overlay_text_tolerates_missing_values():
    assert render.overlay_text({"state": "IDLE"}).startswith("L -/-   -%   -   IDLE")


def test_cues_merge_identical_polls_and_blank_long_gaps():
    timeline = [rec(0), rec(2), rec(4, layer_num=554), rec(60, layer_num=555)]
    cs = [c for c in render.cues(timeline) if c.style == "Default"]
    t = T0.timestamp()
    assert [(c.start - t, c.end - t) for c in cs] == [(0, 4), (4, 14), (60, 70)]
    details = [c for c in render.cues(timeline) if c.style == "Details"]
    assert [(c.start - t, c.end - t) for c in details] == [(0, 14), (60, 70)]  # unchanged across layers
    assert "L 554/" in cs[1].text


def test_build_ass_is_relative_to_segment_start_and_clipped():
    t = T0.timestamp()
    cs = [render.Cue(t, t + 4, "a"), render.Cue(t + 4, t + 3700, "b"), render.Cue(t + 3700, t + 3710, "c")]
    events = [l for l in render.build_ass(cs, t + 2, t + 3602).splitlines() if l.startswith("Dialogue")]
    assert events == ["Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,a",
                      "Dialogue: 0,0:00:02.00,1:01:38.00,Default,,0,0,0,,b"]


needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def make_recording(tmp_path: Path) -> Path:
    seg_dir = tmp_path / "recordings" / "archive_27"
    seg_dir.mkdir(parents=True)
    # A 4 s clip whose timestamps are Unix seconds, like the recorder's -copyts output.
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10",
                    "-t", "4", "-c:v", "mjpeg", "-output_ts_offset", str(T0.timestamp()), "-copyts",
                    str(seg_dir / "20261108T210000Z.mkv")], check=True)
    (tmp_path / "telemetry").mkdir()
    (tmp_path / "telemetry" / "archive_27.jsonl").write_text(
        "".join(json.dumps(rec(s, layer_num=553 + s // 2)) + "\n" for s in (0, 2)))
    return seg_dir


def render_27(tmp_path: Path, **kw) -> Path:
    return render.render(27, tmp_path / "telemetry", tmp_path / "recordings", tmp_path / "renders", **kw)


@needs_ffmpeg
def test_render_end_to_end(tmp_path):
    seg_dir = make_recording(tmp_path)
    out = render_27(tmp_path)
    assert out.name == VIDEO
    assert [p.name for p in (tmp_path / "renders").iterdir()] == [VIDEO]  # only finished videos
    assert render.segment_span(seg_dir / "20261108T210000Z.mkv")[0] == pytest.approx(T0.timestamp())
    probe = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name:format=duration", "-of", "json", str(out)],
        check=True, capture_output=True, text=True).stdout)
    assert probe["streams"][0]["codec_name"] == "h264"
    assert float(probe["format"]["duration"]) == pytest.approx(4, abs=0.2)


@needs_ffmpeg
def test_finished_segments_are_reused_and_growing_ones_rerendered(tmp_path):
    seg = make_recording(tmp_path) / "20261108T210000Z.mkv"
    part = seg.parent / "parts" / "20261108T210000Z.mp4"
    old = time.time() - 3600
    os.utime(seg, (old, old))  # segment closed an hour ago
    render_27(tmp_path)
    first = part.stat().st_mtime_ns
    render_27(tmp_path)
    assert part.stat().st_mtime_ns == first  # reused
    os.utime(seg)  # segment grew since
    render_27(tmp_path)
    assert part.stat().st_mtime_ns != first


@needs_ffmpeg
def test_final_render_deletes_raw_and_parts(tmp_path):
    seg_dir = make_recording(tmp_path)
    out = render_27(tmp_path, final=True)
    assert out.stat().st_size > 0
    assert not seg_dir.exists()
    assert [p.name for p in (tmp_path / "renders").iterdir()] == [VIDEO]


@needs_ffmpeg
def test_render_replaces_a_legacy_named_video(tmp_path):
    make_recording(tmp_path)
    (tmp_path / "renders").mkdir()
    (tmp_path / "renders" / "archive_27.mp4").write_bytes(b"old")
    render_27(tmp_path)
    assert [p.name for p in (tmp_path / "renders").iterdir()] == [VIDEO]


@needs_ffmpeg
def test_failed_final_render_keeps_raw(tmp_path, monkeypatch):
    seg_dir = make_recording(tmp_path)

    def broken(*a, **kw):
        raise subprocess.CalledProcessError(1, "ffmpeg")

    monkeypatch.setattr(render, "render_segment", broken)
    with pytest.raises(subprocess.CalledProcessError):
        render_27(tmp_path, final=True)
    assert any(seg_dir.glob("*.mkv"))


@needs_ffmpeg
def test_each_segment_gets_the_timeline_as_of_its_render(tmp_path, monkeypatch):
    make_recording(tmp_path)
    seg_dir = tmp_path / "recordings" / "archive_27"
    (seg_dir / "20261108T210001Z.mkv").write_bytes((seg_dir / "20261108T210000Z.mkv").read_bytes())
    timeline = tmp_path / "telemetry" / "archive_27.jsonl"
    seen = []

    def fake_render_segment(seg, cues, part):
        seen.append(len(cues))
        with timeline.open("a") as f:  # a poll lands while this segment renders
            f.write(json.dumps(rec(10 + len(seen) * 2, layer_num=600 + len(seen))) + "\n")
        part.write_bytes((seg_dir / "20261108T210000Z.mkv").read_bytes())

    monkeypatch.setattr(render, "render_segment", fake_render_segment)
    monkeypatch.setattr(render.subprocess, "run", lambda *a, **kw: Path(a[0][-1]).write_bytes(b"x"))
    render_27(tmp_path)
    assert seen[1] == seen[0] + 1


def test_chapter_marks_cover_prep_layers_pauses_swaps_and_the_end():
    timeline = [rec(0, layer_num=0, stg_cur_name="Heating heatbed"), rec(2, layer_num=0, stg_cur_name="Heating heatbed"),
                rec(4, layer_num=0, stg_cur_name="Auto bed leveling"),
                rec(6, layer_num=1), rec(8, layer_num=1), rec(10, layer_num=2),
                rec(12, layer_num=2, state="PAUSE"), rec(14, layer_num=2),
                rec(16, layer_num=3, tray_now=3), rec(18, layer_num=3, tray_now=3),
                rec(20, layer_num=3, state="FINISH")]
    t = T0.timestamp()
    assert [(m - t, title) for m, title in render.chapter_marks(timeline)] == [
        (0, "Heating heatbed"), (4, "Auto bed leveling"),
        (6, "Layer 1 / 1415"), (10, "Layer 2 / 1415"), (12, "Layer 2 / 1415 · PAUSE"), (14, "Layer 2 / 1415"),
        (16, "Layer 3 / 1415 · A2 → A4"), (20, "FINISH")]


def test_video_time_skips_recording_gaps():
    spans = [(100.0, 10.0), (200.0, 5.0)]  # 10 s of video, a gap, then 5 s more
    assert [render.video_time(t, spans) for t in (90, 100, 105, 150, 200, 203, 205)] == [0, 0, 5, 10, 10, 13, None]


def test_chapters_keep_the_latest_mark_at_one_moment_and_end_at_the_video_end():
    t = T0.timestamp()
    timeline = [rec(0, layer_num=1), rec(2, layer_num=2), rec(30, layer_num=3), rec(40, layer_num=4)]
    spans = [(t + 2, 10.0), (t + 30, 5.0)]  # recording starts at layer 2; gap from t+12 to t+30
    assert render.chapters(timeline, spans) == [(0, 10.0, "Layer 2 / 1415"), (10.0, 15.0, "Layer 3 / 1415")]


def test_ffmetadata_escapes_titles():
    meta = render.ffmetadata([(0, 1.5, "a=b;c#d")])
    assert meta == ";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1500\ntitle=a\\=b\\;c\\#d\n"


def test_details_text_from_real_poll_and_from_old_telemetry():
    assert render.details_text(rec(0)) == (
        "Matt's P2S\\NApricity-Complete-chain-rod-chaingap · plate 3\\NNozzle 0.2 mm HS00\\N"
        "A2 PLA {\\c&H6E6E6B&}■{\\c} · AMS 19%")
    old = {k: v for k, v in rec(0).items() if k not in ("printer_name", "nozzle_diameter", "tray")}
    assert render.details_text(old) == "Apricity-Complete-chain-rod-chaingap · plate 3"
    assert render.details_text({"current_print": "a{b}\\c"}) == "a(b)/c"


@needs_ffmpeg
def test_render_writes_layer_chapters(tmp_path):
    make_recording(tmp_path)  # layers 553 then 554, 2 s apart
    out = render_27(tmp_path)
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_chapters", "-of", "json", str(out)],
                                      check=True, capture_output=True, text=True).stdout)
    assert [(round(float(c["start_time"])), c["tags"]["title"]) for c in probe["chapters"]] == [
        (0, "Layer 553 / 1415"), (2, "Layer 554 / 1415")]
