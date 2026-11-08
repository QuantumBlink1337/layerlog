"""Re-encode a print's recording with its telemetry burned in.

Each recorded segment is rendered on its own: its first frame's timestamp
(Unix seconds, from the recorder's ``-copyts``) anchors both the clock and an
ASS subtitle track built from the telemetry timeline. Rendering per segment
keeps the overlay aligned across recorder restarts; the parts are joined at
the end without another encode.

``python -m layerlog.surveillance.render ARCHIVE_ID``
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

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

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


@dataclass
class Cue:
    start: float  # Unix seconds
    end: float
    text: str


def load_timeline(path: Path | str) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _n(value: Any, fmt: str = "{:.0f}") -> str:
    return "-" if value is None else fmt.format(value)


def overlay_text(rec: dict[str, Any]) -> str:
    left = rec.get("remaining_min")
    eta = "-" if left is None else f"{int(left) // 60}h{int(left) % 60:02d}m left"
    line1 = (f"L {_n(rec.get('layer'))}/{_n(rec.get('total_layers'))}   {_n(rec.get('progress'))}%   "
             f"{eta}   {rec.get('stage') or rec.get('state') or ''}")
    line2 = (f"Nozzle {_n(rec.get('nozzle_temp'))}/{_n(rec.get('nozzle_target'))}   "
             f"Bed {_n(rec.get('bed_temp'))}/{_n(rec.get('bed_target'))}   "
             f"Chamber {_n(rec.get('chamber_temp'))}   "
             f"Fan P{_n(rec.get('fan_part'))} A{_n(rec.get('fan_aux'))} C{_n(rec.get('fan_chamber'))}")
    return f"{line1}\\N{line2}"


def cues(timeline: list[dict[str, Any]]) -> list[Cue]:
    """One cue per poll, held until the next poll; identical neighbours merged."""
    out: list[Cue] = []
    times = [datetime.fromisoformat(r["ts"]).timestamp() for r in timeline]
    for i, (t, rec) in enumerate(zip(times, timeline)):
        end = min(times[i + 1], t + MAX_HOLD_S) if i + 1 < len(times) else t + MAX_HOLD_S
        text = overlay_text(rec)
        if out and out[-1].text == text and out[-1].end == t:
            out[-1].end = end
        else:
            out.append(Cue(t, end, text))
    return out


def _ass_time(s: float) -> str:
    cs = round(max(s, 0.0) * 100)
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def build_ass(all_cues: list[Cue], t0: float, t1: float) -> str:
    """Subtitle track for a segment whose frames span Unix times ``t0``..``t1``."""
    lines = [f"Dialogue: 0,{_ass_time(c.start - t0)},{_ass_time(c.end - t0)},Default,,0,0,0,,{c.text}"
             for c in all_cues if c.end > t0 and c.start < t1]
    return ASS_HEADER + "\n".join(lines) + "\n"


def segment_span(path: Path) -> tuple[float, float]:
    """Unix time of the segment's first frame, and of its end."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-read_intervals", "%+#1",
         "-show_entries", "packet=pts_time:format=duration", "-of", "json", str(path)],
        check=True, capture_output=True, text=True).stdout
    d = json.loads(out)
    t0 = float(d["packets"][0]["pts_time"])
    duration = float(d.get("format", {}).get("duration") or 0)
    # A segment still being written may report no duration; let it cover everything after t0.
    return t0, t0 + duration if duration > 0 else float("inf")


def _clock_filter(t0: float) -> str:
    return (f"drawtext=text='%{{pts\\:localtime\\:{t0:.3f}\\:%Y-%m-%d %H\\\\\\:%M\\\\\\:%S}}'"
            ":x=20:y=20:fontsize=34:fontcolor=white:box=1:boxcolor=black@0.5:boxborderw=8")


def render_segment(segment: Path, all_cues: list[Cue], out: Path, *, crf: int = 28) -> None:
    t0, t1 = segment_span(segment)
    with tempfile.TemporaryDirectory() as tmp:
        # ffmpeg runs in tmp so the ass filter sees a bare file name (no filtergraph escaping).
        (Path(tmp) / "overlay.ass").write_text(build_ass(all_cues, t0, t1), encoding="utf-8")
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(segment.resolve()),
             "-vf", f"ass=overlay.ass,{_clock_filter(t0)}",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p", "-an",
             str(out.resolve())],
            check=True, cwd=tmp)


def render(archive_id: int, telemetry_dir: Path, recordings_dir: Path, out_dir: Path) -> Path:
    all_cues = cues(load_timeline(telemetry_dir / f"archive_{archive_id}.jsonl"))
    segments = sorted((recordings_dir / f"archive_{archive_id}").glob("*.mkv"))
    if not segments:
        raise FileNotFoundError(f"no recordings for archive {archive_id}")
    parts_dir = out_dir / f"archive_{archive_id}_parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    parts = []
    for seg in segments:
        part = parts_dir / f"{seg.stem}.mp4"
        log.info("rendering %s", seg.name)
        render_segment(seg, all_cues, part)
        parts.append(part)
    final = out_dir / f"archive_{archive_id}.mp4"
    listing = parts_dir / "concat.txt"
    listing.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
    subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-f", "concat",
                    "-safe", "0", "-i", str(listing), "-c", "copy", str(final)], check=True)
    return final


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("archive_id", type=int)
    p.add_argument("--telemetry-dir", type=Path, default=Path("telemetry"))
    p.add_argument("--recordings-dir", type=Path, default=Path("recordings"))
    p.add_argument("--out-dir", type=Path, default=Path("renders"))
    a = p.parse_args()
    print(render(a.archive_id, a.telemetry_dir, a.recordings_dir, a.out_dir))


if __name__ == "__main__":
    main()
