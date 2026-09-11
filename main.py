#!/usr/bin/env python3
"""aidot-viewer -- browse recordings produced by aidot-webrtc.

Read-only web UI over two directory trees:

  DAILY_DIR/<camera>/<YYYYMMDD>.mp4   one consolidated file per camera per day
  RECORD_DIR/<camera>/<ts>.mp4        today's rolling 10-min segments, not yet
                                       consolidated (the newest one may still
                                       be growing -- it streams fine, it's
                                       fragmented mp4)

This app does not write to either tree; aidot-webrtc owns that.
"""
import os
import re
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

DAILY_DIR = os.environ.get("DAILY_DIR", "/daily")
RECORD_DIR = os.environ.get("RECORD_DIR", "/rec")
TZ = timezone(timedelta(hours=int(os.environ.get("TZ_OFFSET_HOURS", "-3"))))

DAY_RE = re.compile(r"^(\d{8})\.mp4$")
SEG_RE = re.compile(r"^(\d{8})-(\d{6})\.mp4$")
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
CHUNK = 1024 * 1024

app = FastAPI(title="aidot-viewer")


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


@app.get("/api/cameras")
def cameras():
    return {"cameras": _cameras()}


@app.get("/api/cameras/{cam}/days")
def days(cam: str):
    """Consolidated daily files, newest first."""
    _safe_cam(cam)
    camdir = os.path.join(DAILY_DIR, cam)
    out = []
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
            })
    out.sort(key=lambda x: x["date"], reverse=True)
    return {"camera": cam, "days": out}


@app.get("/api/cameras/{cam}/today")
def today_segments(cam: str):
    """Today's not-yet-consolidated rolling segments, oldest first."""
    _safe_cam(cam)
    camdir = os.path.join(RECORD_DIR, cam)
    today = datetime.now(TZ).strftime("%Y%m%d")
    out = []
    if os.path.isdir(camdir):
        for f in os.listdir(camdir):
            m = SEG_RE.match(f)
            if not m or m.group(1) != today:
                continue
            p = os.path.join(camdir, f)
            out.append({
                "time": m.group(2),
                "size": os.path.getsize(p),
                "url": f"/media/rec/{cam}/{f}",
            })
    out.sort(key=lambda x: x["time"])
    return {"camera": cam, "date": today, "segments": out}


def _resolve(root, cam, filename, name_re):
    _safe_cam(cam)
    if not name_re.match(filename):
        raise HTTPException(400, "bad filename")
    path = os.path.join(root, cam, filename)
    if not os.path.isfile(path):
        raise HTTPException(404, "not found")
    return path


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
    return _serve_video(_resolve(DAILY_DIR, cam, filename, DAY_RE), request)


@app.get("/media/rec/{cam}/{filename}")
def media_rec(cam: str, filename: str, request: Request):
    return _serve_video(_resolve(RECORD_DIR, cam, filename, SEG_RE), request)


app.mount("/", StaticFiles(directory="static", html=True), name="static")
