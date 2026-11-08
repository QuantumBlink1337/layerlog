# layerlog

Logs Bambu P2S prints to Notion with slicer settings, and keeps a telemetry timeline
per print so a failure can be traced to its layer and moment. Everything is pulled from
Bambuddy's API; nothing needs to reach layerlog.

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

Or on nucleus:

```bash
docker build -t layerlog .
docker run -d --name layerlog-surveillance -v /mnt/user/appdata/layerlog:/data \
  -e BAMBUDDY_URL=http://192.168.0.39:8000 -e TZ=America/Chicago layerlog
```

While a print runs, the camera stream is copied (no re-encode, about 3.7 GB/hour at the
P2S's 1080p ~10 fps) into `RECORDINGS_DIR/archive_<id>/` as hourly segments, and each poll
is appended to `TELEMETRY_DIR/archive_<id>.jsonl`. Set `BAMBUDDY_API_KEY` if Bambuddy auth
is on, `POLL_SECONDS` (default 2) to change the rate, and `RECORD=0` to skip video.

## Render a print with its overlay

```bash
.venv/bin/python -m layerlog.surveillance.render 27   # -> renders/archive_27.mp4
```

Burns in the local clock (`TZ`) and the layer, progress, temperature and fan readings,
re-encoded to H.264 (about 0.5 GB/hour).

## Develop

```bash
uv venv && uv pip install -e '.[dev]' && .venv/bin/pytest
```
