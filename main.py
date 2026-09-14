#!/usr/bin/env python3
"""VIGILANTE -- browse and watch recordings produced by aidot-webrtc.

Read-only web UI over two directory trees:

  DAILY_DIR/<camera>/<YYYYMMDD>.mp4   one consolidated file per camera per day
  RECORD_DIR/<camera>/<ts>.mp4        today's rolling 10-min segments, not yet
                                       consolidated by aidot-webrtc's nightly
                                       job

This app never writes anywhere: it only reads, and serves.

Past days play from their one consolidated file. Today plays straight from the
segments, one after another -- deliberately, not for lack of stitching. A
stitched "today" is stale the moment it is built (it takes minutes by evening,
and the recorder keeps going), so rewinding a minute from the live edge would
land wherever the last stitch happened to stop -- an hour earlier, by evening.
Reading the segments means the most recent one is the one being written right
now, and "a minute ago" is a minute ago.

Camera reconnects mean a segment's actual recorded duration can be shorter
than the wall-clock gap to the next segment's start -- if we just assumed
`wall_clock = day_start + video_position`, that drift compounds over the day
and the scrub bar/clock end up visibly offset from the real time. Instead we
track, per segment, where it lands in the concatenated video (`cum`) and where
it lands on the wall clock (`wall`), and hand that breakpoint table to the
frontend so it can map one to the other exactly.
"""
import collections
import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

DAILY_DIR = os.environ.get("DAILY_DIR", "/daily")
RECORD_DIR = os.environ.get("RECORD_DIR", "/rec")
TZ = timezone(timedelta(hours=int(os.environ.get("TZ_OFFSET_HOURS", "-3"))))
# base URL for mediamtx's built-in WHEP player page, one per camera at
# <MEDIAMTX_URL>/<camera>/ -- must be reachable from the viewer's browser,
# not just from inside this container, so it can't default to 127.0.0.1.
MEDIAMTX_URL = os.environ.get("MEDIAMTX_URL", "")

DAY_RE = re.compile(r"^(\d{8})\.mp4$")
SEG_RE = re.compile(r"^(\d{8})-(\d{6})\.mp4$")
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
CHUNK = 1024 * 1024
# Sanity bound on a segment, used to reject nonsense when a duration is
# derived by subtracting two epochs. aidot-webrtc cuts at SEGMENT_SECONDS
# (600 here); an hour is far past anything it produces and far short of the
# day-sized differences a stale index throws up.
MAX_SEGMENT_SECONDS = 3600

# (path, size) -> probed duration; see _probe_duration. An LRU rather than a
# plain dict: the segment being written right now changes size on every poll,
# so every poll mints a key that will never be looked up again. Evicting the
# oldest keeps those from pushing out the finished segments' entries, which
# are re-read on every request and so stay at the fresh end forever. The
# previous `clear()`-at-a-threshold wiped those too, and the next request then
# re-probed the entire day.
_dur_cache = collections.OrderedDict()
DUR_CACHE_MAX = 5000

app = FastAPI(title="VIGILANTE")


def _cameras():
    names = set()
    for root in (DAILY_DIR, RECORD_DIR):
        if os.path.isdir(root):
            names.update(d for d in os.listdir(root)
                        if os.path.isdir(os.path.join(root, d)))
    return sorted(names)


def _safe_cam(cam):
    if cam not in _cameras():
        raise HTTPException(404, f"unknown camera '{cam}'")
    return cam


def _hhmmss_to_seconds(hhmmss):
    return int(hhmmss[0:2]) * 3600 + int(hhmmss[2:4]) * 60 + int(hhmmss[4:6])


def _probe_duration(path, cache=True):
    """Seconds of media in `path`, or 0.0 if ffprobe can't tell (corrupt stub).

    Memoised on (path, size): a finished segment never changes, so it is only
    ever probed once. The one still being written grows, which changes the key
    and re-probes it -- which is what we want, and it is a single ffprobe.
    Without this, answering "where is 16:37?" would mean probing every segment
    of the day on every seek.
    """
    if cache:
        try:
            key = (path, os.path.getsize(path))
        except OSError:
            return 0.0
        if key in _dur_cache:
            _dur_cache.move_to_end(key)
            return _dur_cache[key]
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=15,
        )
        dur = max(0.0, float(r.stdout.strip()))
    except Exception:
        return 0.0
    if cache:
        _dur_cache[key] = dur
        _dur_cache.move_to_end(key)
        while len(_dur_cache) > DUR_CACHE_MAX:
            _dur_cache.popitem(last=False)
    return dur


def _apart(a, b):
    """Seconds between two times of day, the short way around midnight."""
    d = abs(a - b) % 86400
    return min(d, 86400 - d)


def _build_breaks(camdir, segnames, durs):
    """Map positions in a concatenation of `segnames` to wall-clock time.

    Returns [{cum, wall, dur}]: `cum` seconds into the concatenated video
    correspond to second-of-day `wall`, for `dur` seconds.

    Anchors come from the `.session-*.json` indexes aidot-webrtc's recorder
    writes -- the epoch at which it fed ffmpeg the first frame of a session,
    plus that session's ordered segment list. A segment's true start is then
    `session_start + the duration of everything recorded before it in that
    session`, all measured rather than inferred.

    The filename stamp is only a fallback: ffmpeg writes it when the muxer
    opens the file, which lags the first frame by however long the encoder
    buffered (~12s here), so anchoring to it skews the whole timeline.

    Those same epochs also give us the durations for free, which is what keeps
    this cheap. The recorder assigns a segment's epoch as "where the previous
    one ended" -- it probes the file it has just closed -- so within a session
    the gap between two consecutive epochs *is* the earlier one's duration,
    already measured, at record time, once. Only the last segment of each
    session has nothing after it to subtract from, so only those are probed
    here: ~20 files on the first call for a full day instead of ~140, and all
    but the growing one are closed, so they are probed once and memoised for
    good.
    """
    starts = {}
    epochs = {}     # name -> its raw epoch, for the subtraction below
    session = {}    # name -> which index claimed it, so we only subtract
                    # within a session (across a gap the difference is the
                    # outage, not a duration)
    try:
        indexes = sorted(f for f in os.listdir(camdir) if f.startswith(".session-"))
    except OSError:
        indexes = []
    for idx in indexes:
        try:
            with open(os.path.join(camdir, idx)) as fh:
                sess = json.load(fh)
        except (OSError, ValueError):
            continue
        tz = sess.get("tz_offset", 0)
        if sess.get("starts"):
            # newer recorders write each segment's own epoch, which a deletion
            # elsewhere in the session cannot shift
            for name, epoch in sess["starts"].items():
                starts[name] = (epoch + tz) % 86400
                epochs[name] = epoch
                session[name] = idx
            continue
        offset = 0.0
        for name in sess.get("segments", []):
            starts[name] = (sess["start_epoch"] + offset + tz) % 86400
            if name not in durs:
                durs[name] = _probe_duration(os.path.join(camdir, name))
            offset += durs[name]

    # duration of every segment that has a successor inside its own session
    by_session = {}
    for name, idx in session.items():
        by_session.setdefault(idx, []).append(name)
    for names in by_session.values():
        names.sort(key=epochs.get)
        for name, nxt in zip(names, names[1:], strict=False):
            d = epochs[nxt] - epochs[name]
            # a recorder-side probe that failed leaves two segments claiming
            # the same start, and a stale index can put them a day apart --
            # neither is a duration, so fall through to probing those
            if name not in durs and 0 < d <= MAX_SEGMENT_SECONDS:
                durs[name] = d

    breaks, cum = [], 0.0
    for name in segnames:
        wall = starts.get(name)
        stamp = _hhmmss_to_seconds(name[9:15])
        # The filename stamp lags the first frame by however long the encoder
        # buffered (~12s here), so the index is the better anchor -- but only
        # while it still describes reality. An index whose segment list was
        # rebuilt after the nightly consolidation deleted that session's older
        # files re-anchors the survivors to the session's start, which can be
        # most of a day off. The stamp is never wildly wrong, so it is the
        # referee: disagree with it by minutes and the index has gone stale.
        if wall is None or _apart(wall, stamp) > 300:
            wall = stamp
        d = durs.get(name) or _probe_duration(os.path.join(camdir, name))
        durs[name] = d
        breaks.append({"cum": round(cum, 2), "wall": round(wall, 2), "dur": round(d, 2)})
        cum += d
    return breaks


def _today_segments(cam, date):
    """(time, path, size) for date's real (non-stub) segments, oldest first."""
    camdir = os.path.join(RECORD_DIR, cam)
    out = []
    if os.path.isdir(camdir):
        for f in os.listdir(camdir):
            m = SEG_RE.match(f)
            if not m or m.group(1) != date:
                continue
            p = os.path.join(camdir, f)
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            if size < 4096:  # empty stub from a just-started recorder
                continue
            out.append((m.group(2), p, size))
    out.sort()
    return out


@app.get("/api/cameras")
def cameras():
    return {"cameras": _cameras()}


@app.get("/api/config")
def config():
    return {"mediamtx_url": MEDIAMTX_URL, "live_enabled": bool(MEDIAMTX_URL)}


@app.get("/api/cameras/{cam}/days")
def days(cam: str):
    """One entry per day, newest first.

    A past day is one file (`url`) plus `breaks`, mapping a position in it to
    a wall-clock time-of-day; a `dur` of null there means "runs to the end of
    the file" -- the fallback for days consolidated before aidot-webrtc wrote
    timeline sidecars.

    Today has no single file: it carries `segments` instead, each with the
    second-of-day it starts at and how long it runs, for the player to chain
    through. The last one is still being written, so its `dur` is whatever has
    been recorded as of this request -- poll again for more.
    """
    _safe_cam(cam)
    out = []

    camdir = os.path.join(DAILY_DIR, cam)
    if os.path.isdir(camdir):
        for f in os.listdir(camdir):
            m = DAY_RE.match(f)
            if not m:
                continue
            p = os.path.join(camdir, f)
            # aidot-webrtc drops a <day>.json timeline next to the mp4 saying
            # where each of that day's segments landed in the file and what
            # wall-clock second it started at. Without it all we could do is
            # assume the video starts at 00:00:00, which is wrong for any day
            # that didn't record from midnight -- so fall back to that only
            # for files consolidated before the sidecar existed.
            try:
                with open(os.path.join(camdir, m.group(1) + ".json")) as fh:
                    breaks = json.load(fh)
            except (OSError, ValueError):
                breaks = [{"cum": 0, "wall": 0, "dur": None}]
            out.append({
                "date": m.group(1),
                "size": os.path.getsize(p),
                "url": f"/media/daily/{cam}/{f}",
                "live": False,
                "breaks": breaks,
            })

    today = datetime.now(TZ).strftime("%Y%m%d")
    if not any(d["date"] == today for d in out):
        segs = _today_segments(cam, today)
        if segs:
            names = [os.path.basename(p) for _, p, _ in segs]
            # cheap after the first call: every finished segment's duration is
            # memoised, so this re-probes only the one still growing
            breaks = _build_breaks(os.path.join(RECORD_DIR, cam), names, durs={})
            # The segment being written right now carries no duration of its
            # own: it is muxed fragmented (empty_moov) precisely so it plays
            # before it is closed, which is the same thing as saying its
            # header can't know how long it will be. Take it as running from
            # its start until now -- otherwise the newest few minutes, the
            # ones you actually want when you rewind from live, look absent.
            if breaks and breaks[-1]["dur"] <= 0:
                now = datetime.now(TZ)
                elapsed = (now.hour * 3600 + now.minute * 60 + now.second
                           - breaks[-1]["wall"])
                breaks[-1]["dur"] = round(max(0.0, min(elapsed, 3600.0)), 2)
            out.append({
                "date": today,
                "size": sum(sz for _, _, sz in segs),
                "url": None,
                "live": True,
                "breaks": breaks,
                "segments": [
                    {"url": f"/media/rec/{cam}/{name}",
                     "wall": b["wall"], "dur": b["dur"]}
                    # same length by construction: _build_breaks walks names
                    for name, b in zip(names, breaks, strict=True)
                ],
            })

    out.sort(key=lambda x: x["date"], reverse=True)
    return {"camera": cam, "days": out}


def _serve_video(path, request: Request):
    """Serve a local file with HTTP Range support (206 Partial Content),
    needed for the <video> seek bar. FastAPI's StaticFiles does not do this,
    so it's handled by hand here.
    """
    file_size = os.path.getsize(path)
    range_header = request.headers.get("range")
    start, end = 0, file_size - 1

    if range_header:
        m = RANGE_RE.match(range_header)
        if not m:
            raise HTTPException(416, "invalid Range header")
        if m.group(1):
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else file_size - 1
        else:  # suffix range: bytes=-N
            start = max(0, file_size - int(m.group(2)))
            end = file_size - 1
        if start > end or end >= file_size:
            return Response(status_code=416,
                            headers={"Content-Range": f"bytes */{file_size}"})

    length = end - start + 1

    def stream():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {
        "Content-Length": str(length),
        "Accept-Ranges": "bytes",
        "Content-Type": "video/mp4",
    }
    if range_header:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
        return StreamingResponse(stream(), status_code=206, headers=headers)
    return StreamingResponse(stream(), status_code=200, headers=headers)


@app.get("/media/daily/{cam}/{filename}")
def media_daily(cam: str, filename: str, request: Request):
    _safe_cam(cam)
    if not DAY_RE.match(filename):
        raise HTTPException(400, "bad filename")
    path = os.path.join(DAILY_DIR, cam, filename)
    if not os.path.isfile(path):
        raise HTTPException(404, "not found")
    return _serve_video(path, request)


@app.get("/media/rec/{cam}/{filename}")
def media_rec(cam: str, filename: str, request: Request):
    """One raw segment, including the one currently being recorded -- ffmpeg
    writes these fragmented (empty_moov + frag_keyframe) precisely so they are
    playable before they are closed."""
    _safe_cam(cam)
    if not SEG_RE.match(filename):
        raise HTTPException(400, "bad filename")
    path = os.path.join(RECORD_DIR, cam, filename)
    if not os.path.isfile(path):
        raise HTTPException(404, "not found")
    return _serve_video(path, request)


app.mount("/", StaticFiles(directory="static", html=True), name="static")
