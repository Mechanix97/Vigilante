#!/usr/bin/env python3
"""VIGILANTE -- browse and watch recordings produced by aidot-webrtc.

Read-only web UI over two directory trees:

  DAILY_DIR/<camera>/<YYYYMMDD>.mp4   one consolidated file per camera per day
  RECORD_DIR/<camera>/<ts>.mp4        today's rolling 10-min segments, not yet
                                       consolidated by aidot-webrtc's nightly
                                       job

This app never writes into either tree. It does keep a small ephemeral cache
(CACHE_DIR, defaults to a tmpfs path) of today's segments stitched into one
playable file, rebuilt on demand so "today" scrubs like any other day.
"""
import os
import re
import subprocess
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

DAILY_DIR = os.environ.get("DAILY_DIR", "/daily")
RECORD_DIR = os.environ.get("RECORD_DIR", "/rec")
CACHE_DIR = os.environ.get("CACHE_DIR", "/tmp/vigilante-cache")
TZ = timezone(timedelta(hours=int(os.environ.get("TZ_OFFSET_HOURS", "-3"))))
TODAY_CACHE_MAX_AGE = 45  # seconds; rebuild the "today so far" file if staler
# base URL for mediamtx's built-in WHEP player page, one per camera at
# <MEDIAMTX_URL>/<camera>/ -- must be reachable from the viewer's browser,
# not just from inside this container, so it can't default to 127.0.0.1.
MEDIAMTX_URL = os.environ.get("MEDIAMTX_URL", "")

DAY_RE = re.compile(r"^(\d{8})\.mp4$")
SEG_RE = re.compile(r"^(\d{8})-(\d{6})\.mp4$")
DATE_RE = re.compile(r"^\d{8}$")
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
CHUNK = 1024 * 1024

os.makedirs(CACHE_DIR, exist_ok=True)
_locks = defaultdict(threading.Lock)

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


def _build_today_cache(cam, date):
    """Stitch today's segments into one file under CACHE_DIR, rebuilding it
    if stale. Returns (path, start_seconds) or None if nothing recorded yet.
    """
    segs = _today_segments(cam, date)
    if not segs:
        return None

    key = f"{cam}-{date}"
    cache_path = os.path.join(CACHE_DIR, f"{key}.mp4")
    start_seconds = _hhmmss_to_seconds(segs[0][0])

    with _locks[key]:
        newest_seg_mtime = max(os.path.getmtime(p) for _, p, _ in segs)
        stale = (
            not os.path.isfile(cache_path)
            or os.path.getmtime(cache_path) < newest_seg_mtime
            or (time.time() - os.path.getmtime(cache_path)) > TODAY_CACHE_MAX_AGE
        )
        if stale:
            listfile = os.path.join(CACHE_DIR, f"{key}.txt")
            with open(listfile, "w") as fh:
                for _, p, _ in segs:
                    fh.write(f"file '{p}'\n")
            tmp_out = cache_path + ".tmp"
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
                   "-f", "concat", "-safe", "0", "-i", listfile,
                   "-c", "copy", "-movflags", "+faststart",
                   "-f", "mp4", tmp_out]  # -f mp4: tmp_out's ".tmp" suffix defeats ext-sniffing
            rc = subprocess.run(cmd).returncode
            os.remove(listfile)
            if rc == 0:
                os.replace(tmp_out, cache_path)  # atomic: safe for in-flight reads
            elif not os.path.isfile(cache_path):
                return None

    return cache_path, start_seconds


@app.get("/api/cameras")
def cameras():
    return {"cameras": _cameras()}


@app.get("/api/config")
def config():
    return {"mediamtx_url": MEDIAMTX_URL, "live_enabled": bool(MEDIAMTX_URL)}


@app.get("/api/cameras/{cam}/days")
def days(cam: str):
    """One entry per day: past consolidated files plus today (stitched live
    from segments), newest first. `start_seconds` is the wall-clock second of
    the day the video starts at -- 0 for a full consolidated day, later for
    today (whenever the first segment happened to start).
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
            out.append({
                "date": m.group(1),
                "size": os.path.getsize(p),
                "url": f"/media/daily/{cam}/{f}",
                "start_seconds": 0,
                "live": False,
            })

    today = datetime.now(TZ).strftime("%Y%m%d")
    if not any(d["date"] == today for d in out):
        segs = _today_segments(cam, today)
        if segs:
            out.append({
                "date": today,
                "size": sum(sz for _, _, sz in segs),
                "url": f"/media/today/{cam}/{today}.mp4",
                "start_seconds": _hhmmss_to_seconds(segs[0][0]),
                "live": True,
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


@app.get("/media/today/{cam}/{date}.mp4")
def media_today(cam: str, date: str, request: Request):
    _safe_cam(cam)
    if not DATE_RE.match(date):
        raise HTTPException(400, "bad date")
    built = _build_today_cache(cam, date)
    if not built:
        raise HTTPException(404, "no segments recorded yet for that date")
    path, _start_seconds = built
    return _serve_video(path, request)


app.mount("/", StaticFiles(directory="static", html=True), name="static")
