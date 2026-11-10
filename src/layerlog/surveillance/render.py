"""Re-encode a print's recording with its telemetry burned in.

Each recorded segment is rendered on its own: its first frame's timestamp
(Unix seconds, from the recorder's ``-copyts``) anchors both the clock and an
ASS subtitle track built from the telemetry timeline. Rendering per segment
keeps the overlay aligned across recorder restarts; the parts are joined at
the end without another encode.

The video is named after the print (``2026-11-09 Beeba-baseline-support_PLA #28.mp4``).
Only finished videos ever appear in the renders folder: per-segment parts live
next to the raw footage, and a video being written is a hidden ``.partial`` file,
so a watcher such as Immich never indexes half a render.

``python -m layerlog.surveillance.render ARCHIVE_ID``
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import names

log = logging.getLogger("layerlog.surveillance")

# A poll's text stays up until the next poll, but no longer than this; a longer
# gap in the timeline shows no overlay rather than stale numbers.
MAX_HOLD_S = 10.0

ASS_HEADER = """\
[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, \
Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, \
Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans Mono,34,&H00FFFFFF,&H00FFFFFF,&H80000000,&H80000000,\
0,0,0,0,100,100,0,0,3,8,0,1,20,20,20,1
Style: Details,DejaVu Sans Mono,30,&H00FFFFFF,&H00FFFFFF,&H80000000,&H80000000,\
0,0,0,0,100,100,0,0,3,8,0,9,20,20,20,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


@dataclass
class Cue:
    start: float  # Unix seconds
    end: float
    text: str
    style: str = "Default"  # Default: bottom-left readout; Details: top-right print details


def load_timeline(path: Path | str) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _n(value: Any, fmt: str = "{:.0f}") -> str:
    return "-" if value is None else fmt.format(value)


def overlay_text(rec: dict[str, Any]) -> str:
    left = rec.get("remaining_min")
    eta = "-" if left is None else f"{int(left) // 60}h{int(left) % 60:02d}m left"
    state, stage = rec.get("state") or "", rec.get("stage") or ""
    # The stage name can stay "Printing" through a pause, so any other state is shown too.
    label = stage or state if state in ("RUNNING", "") else f"{state} ({stage})" if stage else state
    line1 = (f"L {_n(rec.get('layer'))}/{_n(rec.get('total_layers'))}   {_n(rec.get('progress'))}%   "
             f"{eta}   {label}")
    line2 = (f"Nozzle {_n(rec.get('nozzle_temp'))}/{_n(rec.get('nozzle_target'))}   "
             f"Bed {_n(rec.get('bed_temp'))}/{_n(rec.get('bed_target'))}   "
             f"Chamber {_n(rec.get('chamber_temp'))}   "
             f"Fan P{_n(rec.get('fan_part'))} A{_n(rec.get('fan_aux'))} C{_n(rec.get('fan_chamber'))}")
    return f"{line1}\\N{line2}"


def _ass_safe(text: str) -> str:
    """Names come from the printer and slicer; braces and backslashes are ASS markup."""
    return text.replace("\\", "/").replace("{", "(").replace("}", ")")


def details_text(rec: dict[str, Any]) -> str:
    """Top-right block: printer, print and plate, nozzle, the slot feeding now.

    Lines with nothing to show are left out, so telemetry from before these
    fields were recorded still gets the print name.
    """
    lines = []
    if rec.get("printer_name"):
        lines.append(_ass_safe(rec["printer_name"]))
    if rec.get("current_print"):
        plate = f" · plate {rec['plate_id']}" if rec.get("plate_id") else ""
        lines.append(_ass_safe(names.model_key(rec["current_print"])) + plate)
    if rec.get("nozzle_diameter"):
        kind = f" {_ass_safe(rec['nozzle_type'])}" if rec.get("nozzle_type") else ""
        lines.append(f"Nozzle {rec['nozzle_diameter']:g} mm{kind}")
    if rec.get("tray"):
        slot = f"{rec['tray']} {_ass_safe(rec.get('filament_type') or '')}".rstrip()
        color = rec.get("filament_color") or ""
        if re.fullmatch(r"[0-9A-Fa-f]{6}", color):  # ASS colours are &HBBGGRR&
            slot += f" {{\\c&H{color[4:6]}{color[2:4]}{color[0:2]}&}}■{{\\c}}"
        if rec.get("ams_humidity") is not None:
            slot += f" · AMS {rec['ams_humidity']}%"
        lines.append(slot)
    return "\\N".join(lines)


def _merged(timeline: list[dict[str, Any]], times: list[float], text_of: Callable[[dict[str, Any]], str],
            style: str) -> list[Cue]:
    out: list[Cue] = []
    for i, (t, rec) in enumerate(zip(times, timeline)):
        end = min(times[i + 1], t + MAX_HOLD_S) if i + 1 < len(times) else t + MAX_HOLD_S
        text = text_of(rec)
        if out and out[-1].text == text and out[-1].end == t:
            out[-1].end = end
        elif text:
            out.append(Cue(t, end, text, style))
    return out


def cues(timeline: list[dict[str, Any]]) -> list[Cue]:
    """One cue per poll and style, held until the next poll; identical neighbours merged."""
    times = [datetime.fromisoformat(r["ts"]).timestamp() for r in timeline]
    return (_merged(timeline, times, overlay_text, "Default")
            + _merged(timeline, times, details_text, "Details"))


def _ass_time(s: float) -> str:
    cs = round(max(s, 0.0) * 100)
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def build_ass(all_cues: list[Cue], t0: float, t1: float) -> str:
    """Subtitle track for a segment whose frames span Unix times ``t0``..``t1``."""
    lines = [f"Dialogue: 0,{_ass_time(c.start - t0)},{_ass_time(c.end - t0)},{c.style},,0,0,0,,{c.text}"
             for c in all_cues if c.end > t0 and c.start < t1]
    return ASS_HEADER + "\n".join(lines) + "\n"


class EmptySegment(ValueError):
    pass


def segment_span(path: Path) -> tuple[float, float]:
    """Unix time of the segment's first frame, and of its end."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-read_intervals", "%+#1",
         "-show_entries", "packet=pts_time:format=duration", "-of", "json", str(path)],
        check=True, capture_output=True, text=True).stdout
    d = json.loads(out)
    if not d.get("packets"):
        raise EmptySegment(f"{path.name} has no frames yet")
    t0 = float(d["packets"][0]["pts_time"])
    duration = float(d.get("format", {}).get("duration") or 0)
    # A segment still being written may report no duration; let it cover everything after t0.
    if duration <= 0:
        return t0, float("inf")
    # With -copyts, ffmpeg 8's Matroska "duration" is the absolute end time, not a length.
    # A real length is hours, never near Unix time, so either form is unambiguous.
    return t0, duration if duration > t0 else t0 + duration


def _clock_filter(t0: float) -> str:
    return (f"drawtext=text='%{{pts\\:localtime\\:{t0:.3f}\\:%Y-%m-%d %H\\\\\\:%M\\\\\\:%S}}'"
            ":x=20:y=20:fontsize=34:fontcolor=white:box=1:boxcolor=black@0.5:boxborderw=8")


def _tmp_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.partial")


# Characters Windows, SMB shares or Immich could trip over in a file name.
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def video_name(archive_id: int, timeline: list[dict[str, Any]]) -> str:
    """``<local start date> <print name> #<archive id>.mp4``, or ``archive_<id>.mp4`` without a name.

    The date leads so a gallery sorts by day; the id keeps reprints of one file apart.
    The first poll that names the print decides, so the name never changes mid-print.
    """
    first = next((r for r in timeline if r.get("current_print")), None)
    if first is None:
        return f"archive_{archive_id}.mp4"
    day = datetime.fromisoformat(first["ts"]).astimezone().strftime("%Y-%m-%d")
    name = _UNSAFE.sub("_", names.model_key(first["current_print"])).strip(" .")
    return f"{day} {name or 'print'} #{archive_id}.mp4"


def video_path(archive_id: int, telemetry_dir: Path, out_dir: Path) -> Path:
    timeline_path = telemetry_dir / f"archive_{archive_id}.jsonl"
    timeline = load_timeline(timeline_path) if timeline_path.exists() else []
    return out_dir / video_name(archive_id, timeline)


def rename_legacy(archive_id: int, telemetry_dir: Path, out_dir: Path) -> Path | None:
    """Rename a render from before print names (``archive_<id>.mp4``); returns the new path."""
    old = out_dir / f"archive_{archive_id}.mp4"
    new = video_path(archive_id, telemetry_dir, out_dir)
    if not old.exists() or new == old:
        return None
    if new.exists():
        raise FileExistsError(new)
    old.rename(new)
    return new


def render_segment(segment: Path, all_cues: list[Cue], out: Path, *, crf: int = 28) -> None:
    t0, t1 = segment_span(segment)
    tmp_out = _tmp_path(out)
    with tempfile.TemporaryDirectory() as tmp:
        # ffmpeg runs in tmp so the ass filter sees a bare file name (no filtergraph escaping).
        (Path(tmp) / "overlay.ass").write_text(build_ass(all_cues, t0, t1), encoding="utf-8")
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(segment.resolve()),
             "-vf", f"ass=overlay.ass,{_clock_filter(t0)}",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p", "-an",
             "-f", "mp4", str(tmp_out.resolve())],
            check=True, cwd=tmp)
    tmp_out.replace(out)  # a player holding the old file keeps it intact


# States the poller writes one line for when a print stops (see telemetry.PARKED_STATES).
_END_STATES = {"FINISH", "FAILED", "IDLE"}


def chapter_marks(timeline: list[dict[str, Any]]) -> list[tuple[float, str]]:
    """(Unix time, title) wherever a new chapter starts.

    One chapter per layer (``Layer 553 / 1415``), plus the preparation stages
    before layer 1, a pause (``Layer 768 · PAUSE``), a filament change
    (``Layer 900 / 1415 · A1 → A3``) and the end state.
    """
    marks: list[tuple[float, str]] = []
    last_key: tuple[Any, ...] | None = None
    last_tray = None
    for r in timeline:
        state, stage, layer, tray = r.get("state") or "", r.get("stage") or "", r.get("layer"), r.get("tray")
        if state in _END_STATES:
            key, title = ("end", state), state
        elif not layer:
            key, title = ("prep", stage), stage or "Starting"
        else:
            paused = state == "PAUSE" or "pause" in stage.lower()
            key = ("layer", layer, paused, tray)
            title = f"Layer {layer} / {r.get('total_layers') or '?'}" + (" · PAUSE" if paused else "")
            if tray and last_tray and tray != last_tray:
                title += f" · {last_tray} → {tray}"
        if key != last_key:
            marks.append((datetime.fromisoformat(r["ts"]).timestamp(), title))
            last_key = key
        last_tray = tray or last_tray
    return marks


def video_time(t: float, spans: list[tuple[float, float]]) -> float | None:
    """Position in the joined video of Unix time ``t``; ``spans`` = (first frame, length) per part.

    A time in a recording gap lands at the start of the next part; after the
    last part there is no position.
    """
    offset = 0.0
    for t0, length in spans:
        if t < t0:
            return offset
        if t < t0 + length:
            return offset + t - t0
        offset += length
    return None


def chapters(timeline: list[dict[str, Any]], spans: list[tuple[float, float]]) -> list[tuple[float, float, str]]:
    """(start, end, title) in video seconds; marks that land on the same moment keep the latest."""
    starts: list[tuple[float, str]] = []
    for t, title in chapter_marks(timeline):
        v = video_time(t, spans)
        if v is None:
            break
        if starts and v - starts[-1][0] < 0.001:
            starts[-1] = (starts[-1][0], title)
        else:
            starts.append((v, title))
    total = sum(length for _, length in spans)
    return [(v, starts[i + 1][0] if i + 1 < len(starts) else total, title) for i, (v, title) in enumerate(starts)]


def _meta_escape(text: str) -> str:
    return re.sub(r"([=;#\\\n])", r"\\\1", text)


def ffmetadata(chaps: list[tuple[float, float, str]]) -> str:
    out = [";FFMETADATA1"]
    for start, end, title in chaps:
        out += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={round(start * 1000)}", f"END={round(end * 1000)}",
                f"title={_meta_escape(title)}"]
    return "\n".join(out) + "\n"


def media_duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                         check=True, capture_output=True, text=True).stdout
    return float(json.loads(out)["format"]["duration"])


def write_chapters(timeline_path: Path, rendered: list[tuple[Path, Path]], dest: Path) -> Path | None:
    """ffmetadata chapters for the joined video, from (segment, part) pairs in order."""
    spans = [(segment_span(seg)[0], media_duration(part)) for seg, part in rendered]
    chaps = chapters(load_timeline(timeline_path), spans)
    if not chaps:
        return None
    dest.write_text(ffmetadata(chaps), encoding="utf-8")
    return dest


def part_is_current(segment: Path, part: Path) -> bool:
    """A part rendered after its segment stopped growing (plus the last poll's hold) is final."""
    return part.exists() and part.stat().st_mtime > segment.stat().st_mtime + MAX_HOLD_S


def render(archive_id: int, telemetry_dir: Path, recordings_dir: Path, out_dir: Path, *,
           final: bool = False) -> Path:
    """Render (or refresh) the print's video in ``out_dir``; finished segments are reused.

    ``final`` is for a print that has ended: once the video is written, the raw
    recording and the per-segment parts (kept inside it) are deleted.
    """
    timeline_path = telemetry_dir / f"archive_{archive_id}.jsonl"
    raw_dir = recordings_dir / f"archive_{archive_id}"
    segments = sorted(raw_dir.glob("*.mkv"))
    if not segments:
        raise FileNotFoundError(f"no recordings for archive {archive_id}")
    parts_dir = raw_dir / "parts"  # not *.mkv, so never taken for a segment
    parts_dir.mkdir(parents=True, exist_ok=True)
    parts = []
    for seg in segments:
        part = parts_dir / f"{seg.stem}.mp4"
        if not part_is_current(seg, part):
            log.info("rendering %s", seg.name)
            try:
                # Re-read per segment: the live one needs every poll up to the moment it's rendered.
                render_segment(seg, cues(load_timeline(timeline_path)), part)
            except EmptySegment as e:
                log.info("skipping %s", e)
                continue
        parts.append((seg, part))
    if not parts:
        raise EmptySegment(f"no frames recorded for archive {archive_id} yet")
    out_dir.mkdir(parents=True, exist_ok=True)
    video = video_path(archive_id, telemetry_dir, out_dir)
    tmp_video = _tmp_path(video)
    listing = parts_dir / "concat.txt"
    listing.write_text("".join(f"file '{p.resolve()}'\n" for _, p in parts))
    chapter_args: list[str] = []
    try:
        meta = write_chapters(timeline_path, parts, parts_dir / "chapters.txt")
        if meta:
            chapter_args = ["-i", str(meta), "-map", "0", "-map_chapters", "1"]
    except Exception:  # chapters are a convenience; never let them cost the video
        log.exception("no chapters for archive %s", archive_id)
    subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-f", "concat",
                    "-safe", "0", "-i", str(listing), *chapter_args, "-c", "copy", "-f", "mp4", str(tmp_video)],
                   check=True)
    tmp_video.replace(video)
    legacy = out_dir / f"archive_{archive_id}.mp4"
    if legacy != video and legacy.exists():
        legacy.unlink()  # an older render of this print, superseded by the one just written
    if final and video.stat().st_size > 0:
        shutil.rmtree(raw_dir)
        log.info("deleted raw recording %s", raw_dir)
    return video


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("archive_id", type=int, nargs="+")
    p.add_argument("--telemetry-dir", type=Path, default=Path("telemetry"))
    p.add_argument("--recordings-dir", type=Path, default=Path("recordings"))
    p.add_argument("--out-dir", type=Path, default=Path("renders"))
    p.add_argument("--rename", action="store_true",
                   help="only rename existing archive_<id>.mp4 renders to the print-name form")
    a = p.parse_args()
    for archive_id in a.archive_id:
        if a.rename:
            new = rename_legacy(archive_id, a.telemetry_dir, a.out_dir)
            print(new or f"archive {archive_id}: nothing to rename")
        else:
            print(render(archive_id, a.telemetry_dir, a.recordings_dir, a.out_dir))


if __name__ == "__main__":
    main()
