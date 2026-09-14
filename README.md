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

VIGILANTE only needs the two directories and the mediamtx URL. A finished day
plays from its one consolidated file; today plays straight from the segments,
chained one into the next, so the newest footage available is the one being
written right now.

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
| `GET /media/rec/{cam}/{file}` | one raw segment, including the one being recorded (HTTP Range supported) |

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `DAILY_DIR` | `/daily` | consolidated daily files |
| `RECORD_DIR` | `/rec` | rolling segments |
| `TZ_OFFSET_HOURS` | `-3` | used to decide which day "today" is; match aidot-camera-webrtc's `TZ` |
| `MEDIAMTX_URL` | — | base URL of mediamtx's WebRTC listener (e.g. `http://homelab:8889`), reachable from the *browser*. Unset hides live view. |

## Notes

* Today is deliberately *not* stitched into one file. It was, at first —
  and a stitched day is stale the moment it is built: by evening the concat
  takes minutes while the recorder keeps going, so rewinding a minute from
  live landed wherever the last stitch happened to stop, over an hour earlier.
  Playing the segments directly costs one `<video>` reload per 10 minutes of
  continuous watching and in exchange "a minute ago" is a minute ago.
* The segment currently being written has no duration in its header (it is
  muxed fragmented, `empty_moov`, precisely so it is playable before it is
  closed), so its length is taken as "from its start until now".
* Segment durations are not probed, they are subtracted. The recorder gives a
  segment the epoch at which the previous one ended, so within a session the
  gap between two consecutive epochs *is* the earlier segment's duration,
  measured once at record time. Only the last segment of a session has nothing
  after it, so only that one is probed — in practice one `ffprobe` per request,
  on the file still being written, whatever the hour. Probing each segment
  instead cost ~17 s by the end of the day (140 segments, ~1.4 GB read), which
  is long enough that the browser never got its timeline.
* What probing is left is memoised on `(path, size)` in a bounded LRU: a
  finished segment is probed once, the growing one re-probed each time it
  changes. The LRU matters — the growing segment mints a key per poll that is
  never read again, and evicting the oldest keeps that churn from displacing
  the entries that are actually reused.
* No thumbnails on the day picker yet.
* Motion detection is deliberately out of scope; the recording layout and this
  app's read-only API are meant to stay stable for a future companion that
  does it.
