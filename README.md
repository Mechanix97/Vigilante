# VIGILANTE

A small web UI to watch the cameras managed by
[aidot-camera-webrtc](https://github.com/Mechanix97/aidot-camera-webrtc) and
scrub back through what they recorded.

* **Dashboard** — every camera live, full screen, side by side.
* **Per-camera tab** — that camera live, with one always-present timeline you
  can drag left to rewind into the day's footage, a day picker, transport
  controls (pause, ±10s, playback speed), and a clock that shows the real
  time of day you are looking at.

It is read-only: both recording trees are mounted read-only and it never
writes into them. Live video is mediamtx's own WebRTC player, embedded.

---

## How it fits together

```
     camera  ──WebRTC──>  aidot-camera-webrtc  ──RTSP──>  mediamtx  ──WebRTC──>  browser
                                   │                                              (live)
                                   ├── /rec/<cam>/<ts>.mp4        rolling segments
                                   └── /daily/<cam>/<day>.mp4     one file per day
                                                │
                                          VIGILANTE  ────────────────────────>  browser
                                                                              (playback)
```

VIGILANTE only needs the two directories and the mediamtx URL. It stitches the
current day's still-accumulating segments into one continuously seekable file
on demand, so "today" scrubs exactly like any finished day.

## The timeline

The hard part of a DVR UI is making a position in a video mean a time of day.
Two things break the naive `time = day_start + position`:

**Recording time is not wall-clock time.** ffmpeg is told `-r fps` on its
rawvideo input, so it assumes every frame it receives is exactly `1/fps` after
the previous one. If frames are only written as they arrive, a camera stall or
a 20-second reconnect produces *no* video for real time that passed, and every
position after it maps to a time that is too early — drift that compounds all
day. aidot-camera-webrtc's recorder therefore paces writes on the wall clock
and repeats the last frame when nothing new arrives, so a segment's duration
equals the real time it covers.

**Filenames lag reality.** ffmpeg stamps a segment's name when the muxer opens
the file, which trails the first frame fed to it by however long the encoder
buffered — measured at ~12 s here. So the recorder also writes a session index
recording the epoch at which it wrote its first frame, plus that session's
ordered segment list. A segment's true start is
`session_start + duration of everything recorded before it in that session` —
measured, not inferred. Consolidated daily files carry the same information in
a `<day>.json` sidecar.

VIGILANTE turns that into a list of `{cum, wall, dur}` breakpoints and maps
positions through it, so the clock stays correct across reconnects, restarts
and gaps. Where no index exists (recordings predating it) it falls back to the
filename stamp.

## Layout it expects

```
DAILY_DIR/<camera>/<YYYYMMDD>.mp4     one consolidated file per day
DAILY_DIR/<camera>/<YYYYMMDD>.json    its timeline sidecar
RECORD_DIR/<camera>/<ts>.mp4          today's rolling segments
RECORD_DIR/<camera>/.session-*.json   recording-session indexes
```

## Usage

```bash
cp .env.example .env     # optional; the defaults below also work as-is
docker compose up -d --build
```

Open `http://<host>:8090`. There is no auth by design — it is meant to sit on
a LAN or a Tailscale network, like the rest of the stack. Put a reverse proxy
in front of it if that ever stops being true.

## API

| Endpoint | Returns |
|---|---|
| `GET /api/cameras` | camera names |
| `GET /api/config` | `{mediamtx_url, live_enabled}` for the frontend |
| `GET /api/cameras/{cam}/days` | one entry per day, newest first, each with its timeline `breaks` |
| `GET /media/daily/{cam}/{file}` | a consolidated day (HTTP Range supported) |
| `GET /media/today/{cam}/{date}.mp4` | today's segments stitched on demand |

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `DAILY_DIR` | `/daily` | consolidated daily files |
| `RECORD_DIR` | `/rec` | rolling segments |
| `CACHE_DIR` | `/tmp/vigilante-cache` | where today's stitched file is kept; ephemeral, rebuilt on demand |
| `TZ_OFFSET_HOURS` | `-3` | used to decide which day "today" is; match aidot-camera-webrtc's `TZ` |
| `MEDIAMTX_URL` | — | base URL of mediamtx's WebRTC listener (e.g. `http://homelab:8889`), reachable from the *browser*. Unset hides live view. |

## Notes

* Stitching today's segments uses a stream copy, which is cheap regardless of
  how long the day is. ffmpeg's concat demuxer can silently truncate (exit
  code 0!) when two adjacent segments carry incompatible H.264 parameter sets
  — which happens when the recorder restarts mid-day — so the result's
  duration is verified against the sum of the parts, and a decode + re-encode
  concat is used as a fallback.
* No thumbnails on the day picker yet.
* Motion detection is deliberately out of scope; the recording layout and this
  app's read-only API are meant to stay stable for a future companion that
  does it.
