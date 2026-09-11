# VIGILANTE

A small web UI to watch and browse the cameras managed by
[aidot-webrtc](https://github.com/Mechanix97/aidot-camera-webrtc):

* **Vivo** -- live view of every camera, via mediamtx's WebRTC player
  (aidot-webrtc re-publishes each camera to mediamtx over RTSP).
* **Grabaciones** -- per-camera daily mp4s and today's not-yet-consolidated
  rolling segments.

It never writes into either recordings tree; both are mounted read-only, and
it doesn't touch mediamtx's state either -- it only embeds its player page.

## Layout it expects (from aidot-webrtc)

```
DAILY_DIR/<camera>/<YYYYMMDD>.mp4    one file per camera per day
RECORD_DIR/<camera>/<HHMMSS'd name>  today's rolling segments (fragmented mp4,
                                      so the newest one streams fine while it
                                      is still being written)
```

## Usage

```bash
docker compose up -d --build
```

Open `http://<host>:8090`. No auth -- meant to sit behind Tailscale/LAN only,
same as the rest of the homelab stack.

## API

| Endpoint | Returns |
|---|---|
| `GET /api/cameras` | camera names |
| `GET /api/config` | `{mediamtx_url, live_enabled}` for the frontend |
| `GET /api/cameras/{cam}/days` | consolidated daily files, newest first |
| `GET /api/cameras/{cam}/today` | today's segments, oldest first |
| `GET /media/daily/{cam}/{file}` | the mp4 itself (206 Range support) |
| `GET /media/rec/{cam}/{file}` | same, for a rolling segment |

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `DAILY_DIR` | `/daily` | consolidated daily files root |
| `RECORD_DIR` | `/rec` | rolling segments root |
| `TZ_OFFSET_HOURS` | `-3` | used only to compute "today" for the segment list; must match aidot-webrtc's `TZ` |
| `MEDIAMTX_URL` | — | base URL of mediamtx's HTTP/WebRTC listener (e.g. `http://homelab:8889`), reachable from the *browser*. Unset disables the Vivo tab. |

## Notes / possible follow-ups

* No thumbnails yet -- the recordings list is text (date/time + file size).
* No auth by design; add a reverse proxy in front if this ever needs to leave
  the LAN.
* Live view is just an iframe to mediamtx's built-in WHEP player page
  (`<MEDIAMTX_URL>/<camera>/`) -- zero custom WebRTC code here.
* A future companion repo is planned for motion-based recording / detection
  (Frigate-style) consuming the same recordings layout or mediamtx's RTSP
  output -- this app's read-only API and directory layout are meant to stay
  stable for that.
