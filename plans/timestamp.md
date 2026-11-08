# Plan: Timestamped print recordings with telemetry overlay

## Goal
Record the P2S camera continuously while it prints, with wall-clock time and live
telemetry burned into the video, so a failure can be traced to the exact moment and
printer state.

## Architecture (runs on nucleus / Unraid)
Two containers:
1. **telemetry-sidecar** (Python): polls Bambuddy every 1–2 s and writes the overlay text to a shared file.
2. **recorder** (ffmpeg): pulls the camera through Bambuddy, burns in the time and overlay text, and writes segmented files.

Shared volume: `/overlay/overlay.txt`, `/recordings/`.

## Sources
- Camera (MJPEG): `GET /api/v1/printers/{id}/camera/stream`
- Snapshot fallback: `GET /api/v1/printers/{id}/camera/snapshot`
- Telemetry: Bambuddy printer status API. **Check the exact endpoint and field names**: layer/total, Z, progress, ETA, nozzle/bed/chamber temps, part/aux/chamber fan %, print stage, filename.
- Reference: https://bambuddy-wiki.pages.dev/features/camera

## Recorder
- ffmpeg input: the MJPEG stream URL. Re-encode to H.264 (`-preset veryfast`, CRF ~28; the stream is low resolution).
- Overlay:
  - `drawtext=text='%{localtime\:%Y-%m-%d %H\\\:%M\\\:%S}'` (top-left)
  - `drawtext=textfile=/overlay/overlay.txt:reload=1` (bottom-left, semi-transparent box)
- Output: `-f segment -segment_time 3600 -strftime 1 /recordings/%Y-%m-%d_%H-%M-%S.mp4`
- Record only while printing: the sidecar starts and stops ffmpeg on print state changes, or ffmpeg runs continuously and idle segments are pruned.

## Telemetry sidecar
- Writes an atomic file (write to a temp file, then rename) so ffmpeg never reads half a line.
- Suggested overlay:
  `L 152/1415  Z 9.22  42%  ETA 18:40 | N 220/220 B 65/65 C 34 | Fan P100 A0 C40 | <stage>`
- Also writes a sidecar CSV/JSONL per print (timestamp + all fields) for later analysis. It doubles as the telemetry log the other thread is building.
- Tags each recording with the print filename and job ID so footage matches the G-code (useful for matching a failure to a G-code layer).

## Open questions to verify first
1. Does the P2S camera serve both Bambuddy's viewer and the recorder at once (through Bambuddy's proxy)? Test with one recorder plus the web UI open.
2. Real frame rate and resolution of the stream on the P2S (the wiki puts P2 in the 30 fps tier).
3. Bambuddy API auth (token?) and rate limits for 1 s polling.
4. Storage: estimate GB per hour at the chosen CRF, and set retention (e.g. keep 14 days, keep failed prints forever).

## Nice-to-haves
- Mark the moment of a pause, error, or HMS code in the overlay and the log.
- Post-print: generate a sped-up copy (e.g. 60x) with the overlay kept, attached to the Bambuddy archive.
- Optional: a second overlay line showing the current G-code feature (support, outer wall, etc.) if Bambuddy exposes it.
