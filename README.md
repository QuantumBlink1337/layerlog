# layerlog

Logs Bambu P2S prints to Notion with slicer settings, and keeps a telemetry timeline
per print so a failure can be traced to its layer and moment. Everything is pulled from
Bambuddy's API; nothing needs to reach layerlog. See `HANDOFF.md` for current state,
decisions and next steps.

Layout:
- `layerlog.bambuddy`, `layerlog.names`: shared Bambuddy client (archives, printer status) and name helpers
- `layerlog.logger`: 3MF parser; will check for finished archives and write them to Notion
- `layerlog.surveillance`: status poller writing `archive_<id>.jsonl` timelines, a camera
  recorder, and a renderer that burns the telemetry into the video; failure point planned
  (see `plans/timestamp.md`)

## Record prints

Needs `ffmpeg` on the PATH (without it, only telemetry is kept).

```bash
BAMBUDDY_URL=http://192.168.0.39:8000 .venv/bin/python -m layerlog.surveillance
```

Or on nucleus (Unraid), with telemetry on the cache and footage on the array:

```bash
docker build --target test .        # runs the test suite against the image's ffmpeg
mkdir -p /mnt/user/appdata/layerlog /mnt/user/layerlog/recordings /mnt/user/layerlog/renders
chown -R 99:100 /mnt/user/appdata/layerlog /mnt/user/layerlog
docker compose up -d --build        # see compose.yaml
```

| Container path | Host path | Holds |
|---|---|---|
| `/data` | `/mnt/user/appdata/layerlog` (cache) | `telemetry/archive_<id>.jsonl` |
| `/recordings` | `/mnt/user/layerlog/recordings` (array) | raw segments, until the final render |
| `/renders` | `/mnt/user/layerlog/renders` (array) | rendered MP4s, kept |

Give the `layerlog` share Array as its primary storage, or raw footage goes to the cache
first. There's also an Unraid template, `unraid/layerlog-surveillance.xml`, if you'd
rather use the Docker tab than compose. On startup, raw recordings of prints that ended
while the container was down get their final render. That only happens once Bambuddy no
longer reports the archive as `printing`.

While a print runs, the camera stream is copied (no re-encode, about 3.7 GB/hour at the
P2S's 1080p ~10 fps) into `RECORDINGS_DIR/archive_<id>/` as hourly segments, and each poll
is appended to `TELEMETRY_DIR/archive_<id>.jsonl`. Set `BAMBUDDY_API_KEY` if Bambuddy auth
is on, `POLL_SECONDS` (default 2) to change the rate, and `RECORD=0` to skip video.

## Rendered video

`RENDERS_DIR/<date> <print name> #<id>.mp4` (default `./renders`), for example
`2026-11-09 Beeba-baseline-support_PLA #28.mp4`, has the local clock (`TZ`) and the
layer, progress, temperature and fan readings burned in, re-encoded to H.264 (about
0.5 GB/hour). The poller renders it automatically:

- every `RENDER_EVERY_SECONDS` (default 3600) while the print runs; finished hourly
  segments are reused, so each refresh only encodes the current hour
- as soon as the print pauses (manual pause, or the printer's error detection pausing it)
- when the print ends (after it has looked ended for 30 s); on success the raw recording
  is **deleted**

The top right shows the printer, print and plate, nozzle, and the AMS slot feeding now
(filament type, a colour swatch, AMS humidity). The file has a chapter per layer
(`Layer 553 / 1415`), with the preparation stages, pauses and filament changes named;
VLC, mpv, IINA, QuickTime and Plex show them, Immich and browsers don't.

The file is replaced atomically, so a copy open in a player is never half-written. The
renders folder only ever holds finished videos (per-segment parts sit in the raw recording
folder; a video being written is a hidden `.partial` file), so it can be a folder Immich
watches. Raw footage and telemetry stay numbered by archive id.

To render by hand (raw footage is kept unless you pass `final=True` from Python), or to
rename renders made before print names to the new form:

```bash
.venv/bin/python -m layerlog.surveillance.render 27
.venv/bin/python -m layerlog.surveillance.render --rename 27 28
```

## Develop

```bash
uv venv && uv pip install -e '.[dev]' && .venv/bin/pytest
```
