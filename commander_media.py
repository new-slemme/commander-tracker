"""Bounded GIF/MP4 conversion and persistent, additive commander-art variants.

Original image URLs stay usable by image-only clients. Metadata lives under the
shared art volume, alongside the assets (include both in backups).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from functools import lru_cache

MAX_MEDIA_BYTES = 30 * 1024 * 1024
MAX_DURATION = 60
_slots = threading.BoundedSemaphore(2)


class MediaError(ValueError):
    pass


def _run(args, timeout):
    try:
        return subprocess.run(args, check=True, capture_output=True, timeout=timeout).stdout
    except FileNotFoundError as exc:
        raise MediaError("Video conversion is unavailable: install FFmpeg on the server.") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError("Art conversion took too long. Try a shorter or smaller animation.") from exc
    except subprocess.CalledProcessError as exc:
        raise MediaError("This animation could not be decoded. Upload a valid GIF or MP4.") from exc


def convert_animation(data: bytes, extension: str) -> tuple[bytes, bytes]:
    """Return silent H.264 MP4 and JPEG poster; never fetch embedded network URLs."""
    if extension.lower() not in {".gif", ".mp4"} or not data:
        raise MediaError("Choose a GIF or MP4 animation.")
    if len(data) > MAX_MEDIA_BYTES:
        raise MediaError("Commander art must be 30 MB or smaller.")
    if not _slots.acquire(blocking=False):
        raise MediaError("Art conversion is busy. Please try again shortly.")
    try:
        with tempfile.TemporaryDirectory(prefix="commander-media-") as tmp:
            source = Path(tmp) / ("input" + extension.lower())
            source.write_bytes(data)
            demuxer = "gif" if extension.lower() == ".gif" else "mov"
            input_args = ["-protocol_whitelist", "file,pipe", "-f", demuxer, "-i", str(source)]
            raw = _run(["ffprobe", "-v", "error", *input_args,
                        "-show_streams", "-show_format", "-of", "json"], 10)
            try:
                info = json.loads(raw)
                stream = next(s for s in info["streams"] if s.get("codec_type") == "video")
                width, height = int(stream["width"]), int(stream["height"])
                duration = float(stream.get("duration") or info["format"].get("duration", 0))
            except (ValueError, KeyError, StopIteration, TypeError) as exc:
                raise MediaError("The file does not contain a readable animation.") from exc
            if not (0 < width <= 4096 and 0 < height <= 4096):
                raise MediaError("Animation dimensions must be no larger than 4096 × 4096.")
            if not math.isfinite(duration) or not 0 < duration <= MAX_DURATION:
                raise MediaError("Animations must be between 0 and 60 seconds long.")
            video, poster = Path(tmp) / "video.mp4", Path(tmp) / "poster.jpg"
            # Keep aspect ratio, never upscale, and use even dimensions for yuv420p.
            scale = min(1, 1280 / max(width, height))
            out_w = max(2, int(width * scale) // 2 * 2)
            out_h = max(2, int(height * scale) // 2 * 2)
            _run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-threads", "2",
                  "-filter_threads", "1", *input_args, "-map", "0:v:0", "-an", "-sn", "-dn",
                  "-map_metadata", "-1", "-t", str(MAX_DURATION),
                  "-vf", f"scale={out_w}:{out_h},setsar=1,fps=30", "-c:v", "libx264",
                  "-threads", "2", "-preset", "veryfast", "-crf", "23",
                  "-profile:v", "main", "-level:v", "4.0", "-pix_fmt", "yuv420p",
                  "-movflags", "+faststart", str(video)], 35)
            _run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-threads", "1",
                  "-filter_threads", "1", "-i", str(video), "-frames:v", "1",
                  "-q:v", "3", "-threads", "1", str(poster)], 5)
            if video.stat().st_size > MAX_MEDIA_BYTES:
                raise MediaError("Converted animation is too large. Try a shorter clip.")
            return video.read_bytes(), poster.read_bytes()
    finally:
        _slots.release()


def _metadata_path(root: Path, source_url: str) -> Path:
    digest = hashlib.sha256(source_url.encode()).hexdigest()
    return root / "video_metadata" / (digest + ".json")


@lru_cache(maxsize=2048)
def _read_metadata(path: Path, mtime_ns: int, size: int) -> dict:
    return json.loads(path.read_text())


def get_variants(root: Path, source_url: str | None) -> dict:
    if not source_url or not source_url.startswith(("/art/", "/media/custom-art/")):
        return {}
    try:
        path = _metadata_path(root, source_url)
        stat = path.stat()
        record = _read_metadata(path, stat.st_mtime_ns, stat.st_size)
        if record.get("source") == source_url and record.get("version") == 1:
            return record
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return {}


def save_variants(root: Path, source_url: str, video_url: str, poster_url: str):
    path = _metadata_path(root, source_url)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"version": 1, "source": source_url, "video_url": video_url, "poster_url": poster_url}
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(record, out)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def remove_metadata(root: Path, source_url: str):
    _metadata_path(root, source_url).unlink(missing_ok=True)
