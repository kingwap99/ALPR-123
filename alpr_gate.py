#!/usr/bin/env python3
"""Lightweight ALPR gate service for a fixed garage camera."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import select
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

import cv2
import numpy as np
import onnxruntime as ort
import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fast_alpr import ALPR
from open_image_models.detection.factory import create_detector
from PIL import Image, ImageDraw, ImageFont


LOG = logging.getLogger("alpr-gate")
NON_ALNUM = re.compile(r"[^A-Z0-9]")
SNAPSHOT_RE = re.compile(r"^[A-Za-z0-9._-]+\.jpg$")
CLIP_RE = re.compile(r"^[A-Za-z0-9._-]+\.mp4$")
VEHICLE_LABELS = {"car", "truck", "bus"}
EVENT_DIRECTIONS = ("enter", "exit", "pass", "stop", "cat", "unknown")
ASSET_DIR = Path(__file__).with_name("assets")
PLATE_FONT_PATH = ASSET_DIR / "fonts" / "TWPlateSans-Bold.ttf"
# Measured on real plates: character pitch is about 0.53 x cap height, which is
# 0.375 x the font size (cap height is 0.7 em). The separator slot is wider.
PLATE_SLOT_RATIO = 0.375
PLATE_SEPARATOR_RATIO = 1.35


def now_local() -> datetime:
    return datetime.now().astimezone().replace(microsecond=0)


def iso_now() -> str:
    return now_local().isoformat()


def normalize_plate(value: str | None) -> str:
    return NON_ALNUM.sub("", (value or "").upper())


def format_plate_display(value: str | None) -> str:
    plate = normalize_plate(value)
    match = re.fullmatch(r"([A-Z]{2,3})([0-9]{4})", plate)
    return f"{match.group(1)}-{match.group(2)}" if match else plate


def plate_is_dark(crop: np.ndarray, box: Any) -> bool:
    """Decide the plate background from the OCR detection patch itself.

    Ordinary cars have white plates with dark glyphs; commercial vehicles
    (e.g. the DHL van) use black plates with white glyphs. The patch is
    mostly background, so a low median brightness means a dark plate.
    """
    height, width = crop.shape[:2]
    x1 = max(0, int(getattr(box, "x1", 0)))
    y1 = max(0, int(getattr(box, "y1", 0)))
    x2 = min(width, int(getattr(box, "x2", 0)))
    y2 = min(height, int(getattr(box, "y2", 0)))
    if x2 - x1 < 8 or y2 - y1 < 4:
        return False
    gray = cv2.cvtColor(crop[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
    return bool(float(np.median(gray)) < 110.0)


def draw_plate_card(
    image: np.ndarray,
    plate_text: str,
    dark: bool = False,
    font_path: Path = PLATE_FONT_PATH,
) -> np.ndarray:
    """Draw a small reference plate using the bundled plate font.

    Set dark=True for the commercial-vehicle style: white glyphs on black.
    """
    if not font_path.is_file() or not plate_text:
        return image
    height, width = image.shape[:2]
    plate_w = max(160, min(360, int(width * 0.36)))
    # Real Taiwan plates are about 368x155 mm, so keep the card near 2.4:1.
    plate_h = max(64, int(plate_w / 2.37))
    margin = max(10, int(height * 0.025))
    card = Image.new("RGBA", (plate_w, plate_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)
    bg = (24, 26, 30, 255) if dark else (247, 250, 255, 255)
    edge = (214, 220, 232, 255) if dark else (155, 168, 187, 255)
    inner = (70, 76, 88, 255) if dark else (214, 223, 235, 255)
    ink = (240, 243, 248, 255) if dark else (11, 19, 34, 255)
    radius = max(6, int(plate_h * 0.07))
    draw.rounded_rectangle((2, 2, plate_w - 3, plate_h - 3), radius=radius, fill=bg, outline=edge, width=max(2, plate_h // 45))
    inset = max(5, plate_h // 18)
    draw.rounded_rectangle((inset, inset, plate_w - inset - 1, plate_h - inset - 1), radius=max(4, radius - 3), outline=inner, width=1)
    # Real plates use one fixed slot per character, so a narrow glyph such as
    # "1" sits in the same pitch as the letters. Proportional advances made the
    # digits bunch up, so place every glyph in its own slot instead.
    chars = list(plate_text)
    size = max(12, int(plate_h * 0.60))
    while size > 8:
        font = ImageFont.truetype(str(font_path), size)
        slots = [size * PLATE_SLOT_RATIO * (PLATE_SEPARATOR_RATIO if c == "-" else 1.0) for c in chars]
        box = draw.textbbox((0, 0), plate_text, font=font)
        if sum(slots) <= plate_w * 0.88 and box[3] - box[1] <= plate_h * 0.58:
            break
        size -= 2
    cursor = plate_w / 2 - sum(slots) / 2
    for char, slot in zip(chars, slots):
        if char == "-":
            # The plate separator is a small centred dot, not a full hyphen.
            dot_w = max(3, round(size * 0.11))
            dot_h = max(3, round(size * 0.09))
            cx = cursor + slot / 2
            cy = plate_h * 0.52
            draw.rounded_rectangle(
                (cx - dot_w / 2, cy - dot_h / 2, cx + dot_w / 2, cy + dot_h / 2),
                radius=dot_h / 2,
                fill=ink,
            )
        else:
            draw.text((cursor + slot / 2, plate_h * 0.52), char, font=font, fill=ink, anchor="mm")
        cursor += slot
    base = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB)).convert("RGBA")
    base.paste(card, (margin, height - plate_h - margin), card)
    return cv2.cvtColor(np.array(base.convert("RGB")), cv2.COLOR_RGB2BGR)


def parse_roi(value: Any) -> Optional[tuple[float, float, float, float]]:
    if not value:
        return None
    try:
        if isinstance(value, dict):
            x = float(value.get("x", 0))
            y = float(value.get("y", 0))
            w = float(value.get("w", value.get("width")))
            h = float(value.get("h", value.get("height")))
        else:
            x, y, w, h = (float(v) for v in value)
    except (KeyError, TypeError, ValueError):
        return None
    x = max(0.0, min(0.98, x))
    y = max(0.0, min(0.98, y))
    w = max(0.02, min(1.0 - x, w))
    h = max(0.02, min(1.0 - y, h))
    if w < 0.02 or h < 0.02:
        return None
    return (round(x, 4), round(y, 4), round(w, 4), round(h, 4))


def safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


STREAM_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def parse_stream_name(value: Any) -> Optional[str]:
    """Sanitize a go2rtc stream name before it becomes part of a URL."""
    name = str(value or "").strip()
    return name if STREAM_NAME_RE.fullmatch(name) else None


def strip_credentials(url: str) -> str:
    """Remove user:password from a URL before exposing it to the UI."""
    return re.sub(r"://[^@/]+@", "://***@", url)


def rtsp_auth(url: str) -> tuple[str, str]:
    """Return (user, password) embedded in an RTSP URL, URL-decoded."""
    try:
        parts = urllib.parse.urlparse(url)
        return urllib.parse.unquote(parts.username or ""), urllib.parse.unquote(parts.password or "")
    except ValueError:
        return "", ""


def boxes_overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    """True when two boxes overlap enough to be treated as the same object."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix = max(0, min(ax2, bx2) - max(ax1, bx1))
    iy = max(0, min(ay2, by2) - max(ay1, by1))
    inter = ix * iy
    area_a = max(1, (ax2 - ax1) * (ay2 - ay1))
    area_b = max(1, (bx2 - bx1) * (by2 - by1))
    ratio = inter / min(area_a, area_b)
    return ratio > 0.35


def cat_confirmed(state: Any, box: tuple[int, int, int, int], now: float) -> bool:
    """Require two nearby cat hits in consecutive checks before trusting one.

    Corrupted H.265 frames show mosaic blocks that the detector reads as cats;
    those blocks jump position between checks, while a real cat stays put. The
    pending box is kept after confirmation so a lingering cat keeps confirming,
    and callers clear it on frames without any cat hit.
    """
    prev = getattr(state, "pending_cat", None)
    if prev is None or now - prev[1] > 12.0:
        state.pending_cat = (box, now)
        return False
    if not boxes_overlap(prev[0], box):
        state.pending_cat = (box, now)
        return False
    state.pending_cat = (box, now)
    return True


def probe_video_codec(ffprobe: str, path: Path, timeout: float = 10.0) -> str:
    """Return the video codec name of a local media file, or empty on error."""
    cmd = [
        ffprobe,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name",
        "-of", "csv=p=0",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except Exception:
        return ""
    return proc.stdout.decode("utf-8", "replace").strip().lower()


def probe_stream(ffprobe: str, rtsp_url: str, timeout: float = 12.0) -> dict[str, Any]:
    """Probe an RTSP source and report its video size and frame rate."""
    cmd = [
        ffprobe,
        "-v", "error",
        "-rtsp_transport", "tcp",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate",
        "-of", "json",
        rtsp_url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "連線逾時（12 秒）"}
    except OSError as exc:
        return {"ok": False, "error": f"ffprobe 無法執行：{exc}"}
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        return {"ok": False, "error": strip_credentials(err)[:160] or "ffprobe 失敗"}
    try:
        info = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": "ffprobe 輸出無法解析"}
    streams = info.get("streams") or []
    if not streams:
        return {"ok": False, "error": "沒有視訊串流"}
    first = streams[0]
    width = safe_int(first.get("width"), 0)
    height = safe_int(first.get("height"), 0)
    try:
        num, den = str(first.get("r_frame_rate") or "0/1").split("/")
        fps = round(float(num) / float(den), 2) if float(den) else 0.0
    except ValueError:
        fps = 0.0
    if width < 320 or height < 240:
        return {"ok": False, "error": f"解析度過小（{width}x{height}）"}
    return {"ok": True, "width": width, "height": height, "fps": fps}


def parse_retention(value: Any) -> Optional[int]:
    """Return a valid snapshot retention in days, or None."""
    days = safe_int(value, 0)
    if 1 <= days <= 365:
        return days
    return None


RECORD_MODES = ("all", "crossing")


def parse_record_mode(value: Any) -> str:
    """Return the event recording mode: all activity, or gate crossings only."""
    mode = str(value or "").strip().lower()
    return mode if mode in RECORD_MODES else "all"


def safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class CameraCfg:
    name: str
    rtsp_url: str
    width: int
    height: int
    fps: float
    ffmpeg: str
    startup_timeout: float
    read_timeout: float


@dataclass
class ModelCfg:
    vehicle: str
    vehicle_conf: float
    plate_detector: str
    plate_ocr: str
    min_ocr: float
    max_vehicles: int
    threads: int
    # Small, distant "plates" are unreliable: a 39x32 px patch of a parked
    # scooter read as a plate at 0.99 confidence. Real plates here are ~200x90.
    min_plate_px: int = 60
    # CoreML can deadlock while compiling the plate model on some boots; the
    # CPU provider is slower but always starts.
    disable_coreml: bool = False


@dataclass
class GateCfg:
    line_y: int
    margin: int
    min_track_seconds: float
    dedupe_seconds: float
    votes: int
    pending_timeout: float
    track_max_age: float
    enter_direction: str = "up"
    record_mode: str = "all"
    match_seconds: float = 12.0


@dataclass
class CameraProfile:
    id: str
    label: str
    camera: CameraCfg
    line_y: int
    margin: int
    enter_direction: str
    live_url: str
    roi: Optional[tuple[float, float, float, float]] = None
    clip_url: Optional[str] = None
    # Cat sentry flag: watched cameras run a low-rate cat-only pipeline.
    # The active camera checks cats inside the main 2 fps pipeline instead.
    cat_watch: bool = False


@dataclass
class MotionCfg:
    diff_threshold: float
    idle_poll_seconds: float


@dataclass
class AnimalCfg:
    cat: bool
    cat_conf: float
    cat_dedupe_seconds: float
    cat_leave_seconds: float
    cat_clip_max_seconds: float


@dataclass
class HaCfg:
    webhook_url: str
    public_base_url: str
    timeout: float
    retries: int


@dataclass
class StorageCfg:
    data_dir: Path
    retain_days: int


@dataclass
class ApiCfg:
    host: str
    port: int


@dataclass
class PlateCfg:
    allow: set[str]
    pattern: re.Pattern[str]
    emit_unknown: bool


@dataclass
class Config:
    camera: CameraCfg
    models: ModelCfg
    gate: GateCfg
    cameras: dict[str, CameraProfile]
    active_camera: str
    motion: MotionCfg
    animals: AnimalCfg
    ha: HaCfg
    storage: StorageCfg
    api: ApiCfg
    plates: PlateCfg
    go2rtc_api_url: str
    go2rtc_rtsp_base: str
    raw: dict[str, Any]


def load_config(path: Path) -> Config:
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    cam = raw.get("camera") or {}
    models = raw.get("models") or {}
    gate = raw.get("gate") or {}
    motion = raw.get("motion") or {}
    ha = raw.get("ha") or {}
    storage = raw.get("storage") or {}
    api = raw.get("api") or {}
    plates = raw.get("plates") or {}
    animals = raw.get("animals") or {}

    rtsp_url = str(cam.get("rtsp_url") or "").strip()
    if not rtsp_url.startswith("rtsp://"):
        raise ValueError("camera.rtsp_url must be an rtsp:// URL")

    width = safe_int(cam.get("width"), 1264)
    height = safe_int(cam.get("height"), 1264)
    if width < 320 or height < 320:
        raise ValueError("camera width/height are too small")

    base_ffmpeg = str(cam.get("ffmpeg") or "/opt/homebrew/bin/ffmpeg")
    base_startup = max(5.0, safe_float(cam.get("startup_timeout"), 30.0))
    base_read = max(2.0, safe_float(cam.get("read_timeout"), 8.0))

    def build_camera(camera_id: str, item: dict[str, Any], default_width: int, default_height: int) -> CameraCfg:
        url = str(item.get("rtsp_url") or rtsp_url).strip()
        if not url.startswith("rtsp://"):
            raise ValueError(f"camera {camera_id}: rtsp_url must be an rtsp:// URL")
        return CameraCfg(
            name=camera_id,
            rtsp_url=url,
            width=safe_int(item.get("width"), default_width),
            height=safe_int(item.get("height"), default_height),
            fps=max(0.5, safe_float(item.get("fps"), safe_float(cam.get("fps"), 2.0))),
            ffmpeg=str(item.get("ffmpeg") or base_ffmpeg),
            startup_timeout=max(5.0, safe_float(item.get("startup_timeout"), base_startup)),
            read_timeout=max(2.0, safe_float(item.get("read_timeout"), base_read)),
        )

    web = raw.get("web") or {}
    base_live_url = str(web.get("live_url") or "http://127.0.0.1:1984/stream.html?src=garage_h264")
    # go2rtc endpoints are derived from the live URL host so the camera
    # panel can list streams and probe new sources without extra config.
    go2rtc_api_url = str(web.get("go2rtc_api") or "").strip().rstrip("/")
    go2rtc_rtsp_base = str(web.get("go2rtc_rtsp") or "").strip().rstrip("/")
    if not go2rtc_api_url:
        try:
            live_parts = urllib.parse.urlparse(base_live_url)
            if live_parts.scheme.startswith("http"):
                go2rtc_api_url = f"{live_parts.scheme}://{live_parts.netloc}"
        except ValueError:
            go2rtc_api_url = "http://127.0.0.1:1984"
    if not go2rtc_rtsp_base:
        try:
            first_rtsp = urllib.parse.urlparse(str(cam.get("rtsp_url") or ""))
            host = first_rtsp.hostname or "127.0.0.1"
            port = first_rtsp.port or 8554
            go2rtc_rtsp_base = f"rtsp://{host}:{port}"
        except ValueError:
            go2rtc_rtsp_base = "rtsp://127.0.0.1:8554"
    cameras_raw = raw.get("cameras") or {}
    profiles: dict[str, CameraProfile] = {}
    if isinstance(cameras_raw, dict) and cameras_raw:
        for camera_id, item in cameras_raw.items():
            if not isinstance(item, dict):
                continue
            camera = build_camera(str(camera_id), item, width, height)
            profiles[str(camera_id)] = CameraProfile(
                id=str(camera_id),
                label=str(item.get("label") or camera_id),
                camera=camera,
                line_y=max(0, min(camera.height - 1, safe_int(item.get("line_y"), int(camera.height * 0.79)))),
                margin=max(5, safe_int(item.get("margin"), safe_int(gate.get("margin"), 25))),
                enter_direction=str(item.get("enter_direction") or gate.get("enter_direction") or "up").lower(),
                live_url=str(item.get("live_url") or base_live_url),
                roi=parse_roi(item.get("roi")),
                clip_url=str(item.get("clip_url") or "").strip() or None,
            )
    if not profiles:
        camera_id = str(cam.get("name") or "garage")
        camera = build_camera(camera_id, cam, width, height)
        profiles[camera_id] = CameraProfile(
            id=camera_id,
            label=str(cam.get("label") or camera_id),
            camera=camera,
            line_y=max(0, min(height - 1, safe_int(gate.get("line_y"), int(height * 0.79)))),
            margin=max(5, safe_int(gate.get("margin"), 25)),
            enter_direction=str(gate.get("enter_direction") or "up").lower(),
            live_url=base_live_url,
            roi=parse_roi(raw.get("roi")),
            clip_url=str(cam.get("clip_url") or "").strip() or None,
        )
    active_camera = str(raw.get("active_camera") or next(iter(profiles)))
    if active_camera not in profiles:
        active_camera = next(iter(profiles))
    active_profile = profiles[active_camera]

    cfg = Config(
        camera=active_profile.camera,
        models=ModelCfg(
            vehicle=str(models.get("vehicle") or "rf-detr-nano-384-coco"),
            vehicle_conf=safe_float(models.get("vehicle_conf"), 0.35),
            plate_detector=str(models.get("plate_detector") or "yolo-v9-t-384-license-plate-end2end"),
            plate_ocr=str(models.get("plate_ocr") or "cct-xs-v2-global-model"),
            min_ocr=safe_float(models.get("min_ocr"), 0.90),
            max_vehicles=max(1, min(5, safe_int(models.get("max_vehicles"), 3))),
            threads=max(1, min(4, safe_int(models.get("threads"), 2))),
            min_plate_px=max(24, safe_int(models.get("min_plate_px"), 60)),
            disable_coreml=bool(models.get("disable_coreml", False)),
        ),
        gate=GateCfg(
            line_y=active_profile.line_y,
            margin=active_profile.margin,
            min_track_seconds=max(0.0, safe_float(gate.get("min_track_seconds"), 0.8)),
            dedupe_seconds=max(1.0, safe_float(gate.get("dedupe_seconds"), 60.0)),
            votes=max(1, safe_int(gate.get("votes"), 2)),
            pending_timeout=max(2.0, safe_float(gate.get("pending_timeout"), 8.0)),
            track_max_age=max(2.0, safe_float(gate.get("track_max_age"), 12.0)),
            enter_direction=active_profile.enter_direction,
            record_mode=parse_record_mode(gate.get("record_mode")),
            match_seconds=max(1.0, safe_float(gate.get("match_seconds"), 12.0)),
        ),
        cameras=profiles,
        active_camera=active_camera,
        motion=MotionCfg(
            diff_threshold=max(0.2, safe_float(motion.get("diff_threshold"), 2.5)),
            idle_poll_seconds=max(2.0, safe_float(motion.get("idle_poll_seconds"), 8.0)),
        ),
        animals=AnimalCfg(
            cat=bool(animals.get("cat", True)),
            cat_conf=max(0.1, safe_float(animals.get("cat_conf"), 0.60)),
            cat_dedupe_seconds=max(0.0, safe_float(animals.get("cat_dedupe_seconds"), 600.0)),
            cat_leave_seconds=max(1.0, safe_float(animals.get("cat_leave_seconds"), 4.0)),
            cat_clip_max_seconds=max(10.0, safe_float(animals.get("cat_clip_max_seconds"), 180.0)),
        ),
        ha=HaCfg(
            webhook_url=str(ha.get("webhook_url") or "").strip(),
            public_base_url=str(ha.get("public_base_url") or "").strip().rstrip("/"),
            timeout=max(1.0, safe_float(ha.get("timeout"), 3.0)),
            retries=max(1, safe_int(ha.get("retries"), 3)),
        ),
        storage=StorageCfg(
            data_dir=Path(str(storage.get("data_dir") or "~/Library/Application Support/alpr-gate")).expanduser(),
            retain_days=max(1, safe_int(storage.get("retain_days"), 7)),
        ),
        api=ApiCfg(
            host=str(api.get("host") or "0.0.0.0"),
            port=max(1024, min(65535, safe_int(api.get("port"), 8099))),
        ),
        plates=PlateCfg(
            allow={normalize_plate(v) for v in (plates.get("allow") or []) if normalize_plate(v)},
            pattern=re.compile(str(plates.get("pattern") or r"^[A-Z]{2,3}[0-9]{4}$")),
            emit_unknown=bool(plates.get("emit_unknown", False)),
        ),
        go2rtc_api_url=go2rtc_api_url,
        go2rtc_rtsp_base=go2rtc_rtsp_base,
        raw=raw,
    )
    return cfg


def setup_logging(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    from logging.handlers import RotatingFileHandler

    file_handler = RotatingFileHandler(
        data_dir / "alpr.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)


class Capture:
    """Persistent ffmpeg RTSP reader that emits BGR frames."""

    def __init__(self, cfg: CameraCfg) -> None:
        self.cfg = cfg
        self.frame_bytes = cfg.width * cfg.height * 3
        self.proc: Optional[subprocess.Popen[bytes]] = None
        self.stderr_thread: Optional[threading.Thread] = None
        self.last_error = ""

    def start(self) -> None:
        self.stop()
        cmd = [
            self.cfg.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-rtsp_transport",
            "tcp",
            "-i",
            self.cfg.rtsp_url,
            "-vf",
            f"fps={self.cfg.fps},scale={self.cfg.width}:{self.cfg.height}",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "-",
        ]
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self.stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self.stderr_thread.start()
        LOG.info("ffmpeg started for camera %s", self.cfg.name)

    def _drain_stderr(self) -> None:
        proc = self.proc
        if not proc or not proc.stderr:
            return
        while True:
            line = proc.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").strip()
            if text:
                text = re.sub(r"rtsp://[^@\s]+@", "rtsp://***@", text)
                self.last_error = text[:500]
                LOG.warning("ffmpeg: %s", text[:500])

    def _read_exact(self, size: int, proc: subprocess.Popen[bytes]) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            if not proc.stdout:
                break
            chunk = os.read(proc.stdout.fileno(), remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def read(self, timeout: float) -> Optional[np.ndarray]:
        proc = self.proc
        if not proc or not proc.stdout:
            return None
        ready, _, _ = select.select([proc.stdout], [], [], timeout)
        if not ready:
            return None
        data = self._read_exact(self.frame_bytes, proc)
        if len(data) != self.frame_bytes:
            raise RuntimeError(f"short frame from ffmpeg ({len(data)}/{self.frame_bytes})")
        return np.frombuffer(data, dtype=np.uint8).reshape(self.cfg.height, self.cfg.width, 3)

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if not proc:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


class ClipRecorder:
    """Record one clip by copying the stream; transcode HEVC to H.264 on stop."""

    def __init__(self, ffmpeg: str, url: str, path: Path, max_seconds: float) -> None:
        self.ffmpeg = ffmpeg
        self.url = url
        self.path = path
        self.max_seconds = max_seconds
        self.proc: Optional[subprocess.Popen[bytes]] = None

    def start(self) -> bool:
        try:
            self.proc = subprocess.Popen(
                [
                    self.ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-rtsp_transport",
                    "tcp",
                    "-i",
                    self.url,
                    "-t",
                    f"{self.max_seconds:.0f}",
                    "-c",
                    "copy",
                    "-movflags",
                    "+faststart",
                    "-y",
                    str(self.path),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception as exc:
            LOG.warning("clip recorder failed to start: %s", exc)
            self.proc = None
            return False

    def stop(self) -> None:
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=6)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        self.ensure_browser_codec()

    def ensure_browser_codec(self) -> None:
        """Browsers cannot play HEVC; transcode the clip with VideoToolbox if needed."""
        ffprobe = str(Path(self.ffmpeg).with_name("ffprobe"))
        codec = probe_video_codec(ffprobe, self.path)
        if codec not in ("hevc", "h265"):
            return
        tmp = self.path.with_suffix(".h264.mp4")
        cmd = [
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-i", str(self.path),
            "-c:v", "h264_videotoolbox", "-b:v", "4M",
            "-pix_fmt", "yuv420p",
            "-an",
            "-movflags", "+faststart",
            "-y", str(tmp),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=180)
        except Exception as exc:
            LOG.warning("clip transcode failed for %s: %s", self.path.name, exc)
            tmp.unlink(missing_ok=True)
            return
        if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size > 4096:
            os.replace(tmp, self.path)
            LOG.info("clip transcoded hevc->h264: %s", self.path.name)
        else:
            LOG.warning(
                "clip transcode rejected for %s: %s",
                self.path.name,
                proc.stderr.decode("utf-8", "replace").strip()[:200],
            )
            tmp.unlink(missing_ok=True)


class CatSentinel:
    """Low-rate cat watcher for one non-active camera.

    Cats linger for tens of seconds, so 1 fps at 640x360 is plenty. Motion is
    gated first and the shared detector runs behind a lock, so idle sentinels
    cost only ffmpeg decoding and never compete with the main ALPR pipeline.
    """

    FPS = 1.0
    WIDTH = 640
    HEIGHT = 360

    def __init__(self, service: "Service", profile: CameraProfile) -> None:
        self.service = service
        self.profile = profile
        self.camera_cfg = CameraCfg(
            name=profile.camera.name,
            rtsp_url=profile.camera.rtsp_url,
            width=self.WIDTH,
            height=self.HEIGHT,
            fps=self.FPS,
            ffmpeg=profile.camera.ffmpeg,
            startup_timeout=profile.camera.startup_timeout,
            read_timeout=max(4.0, profile.camera.read_timeout),
        )
        self.capture = Capture(self.camera_cfg)
        self.motion = MotionGate(service.cfg.motion)
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.last_cat_seen = 0.0
        self.last_cat_at = 0.0
        self.pending_cat: Optional[tuple[tuple[int, int, int, int], float]] = None
        self.clip: Optional[ClipRecorder] = None
        self.clip_started = 0.0
        self.clip_name = ""
        self.clip_poster = ""
        self.clip_conf = 0.0
        self.clip_ts = ""

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.capture.start()
        self.thread = threading.Thread(target=self._run, name=f"cat-{self.profile.id}", daemon=True)
        self.thread.start()
        LOG.info("cat sentinel started: %s", self.profile.id)

    def stop(self) -> None:
        self.stop_event.set()
        if self.clip is not None:
            try:
                self._finish_clip(time.monotonic())
            except Exception as exc:
                LOG.warning("sentinel %s clip finish failed: %s", self.profile.id, exc)
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)
        # Stop capture after the worker exits; its restart path would
        # otherwise respawn ffmpeg right after we killed it.
        self.capture.stop()
        LOG.info("cat sentinel stopped: %s", self.profile.id)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                frame = self.capture.read(self.camera_cfg.read_timeout)
                if frame is None:
                    raise RuntimeError("no frame from ffmpeg")
                now = time.monotonic()
                should, _diff = self.motion.should_process(frame, now)
                if should:
                    self._check(frame, now)
                self._maybe_finish_clip(now)
            except Exception as exc:
                LOG.warning("cat sentinel %s: %s", self.profile.id, str(exc)[:200])
                self.capture.stop()
                self.stop_event.wait(5.0)
                if not self.stop_event.is_set():
                    try:
                        self.capture.start()
                    except Exception as start_exc:
                        LOG.warning("cat sentinel %s restart failed: %s", self.profile.id, start_exc)
                        self.stop_event.wait(5.0)

    def _check(self, frame: np.ndarray, now: float) -> None:
        if not self.service.cfg.animals.cat:
            return
        detector = self.service.vehicle_detector
        if detector is None:
            return
        with self.service.infer_lock:
            detections = detector.predict(frame)
        best: Optional[tuple[float, tuple[int, int, int, int]]] = None
        height, width = frame.shape[:2]
        for det in detections:
            if str(det.label).lower() != "cat":
                continue
            conf = float(det.confidence)
            if conf < self.service.cat_conf:
                continue
            box = det.bounding_box
            x1 = max(0, int(box.x1))
            y1 = max(0, int(box.y1))
            x2 = min(width, int(box.x2))
            y2 = min(height, int(box.y2))
            if x2 - x1 < 20 or y2 - y1 < 20:
                continue
            if best is None or conf > best[0]:
                best = (conf, (x1, y1, x2, y2))
        if best is None:
            self.pending_cat = None
            return
        conf, box = best
        if not cat_confirmed(self, box, now):
            return
        self.last_cat_seen = now
        if self.clip is None and now - self.last_cat_at >= self.service.cfg.animals.cat_dedupe_seconds:
            self._start_clip(frame, box, conf, now)

    def _poster_name(self, ts: str, confidence: float) -> str:
        return f"{ts}-{self.profile.id}-cat-{int(round(confidence * 100)):03d}.jpg"

    def _start_clip(self, frame: np.ndarray, box: tuple[int, int, int, int], confidence: float, now: float) -> None:
        ts = now_local().strftime("%Y%m%d-%H%M%S")
        poster = self._poster_name(ts, confidence)
        try:
            image = frame.copy()
            x1, y1, x2, y2 = box
            cv2.rectangle(image, (x1, y1), (x2, y2), (0, 165, 255), 2)
            cv2.putText(image, f"CAT {confidence:.2f}", (max(6, x1), max(22, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 165, 255), 2)
            path = self.service.snapshot_dir / poster
            cv2.imwrite(str(path), image, [cv2.IMWRITE_JPEG_QUALITY, 86])
        except Exception as exc:
            LOG.warning("sentinel %s poster failed: %s", self.profile.id, exc)
            poster = ""
        clip_url = (self.profile.clip_url or self.profile.camera.rtsp_url or "").strip()
        if not clip_url:
            self.last_cat_at = now
            self.service._store_cat_event(iso_now(), confidence, poster, "", camera=self.profile.id)
            return
        name = f"{ts}-{self.profile.id}-cat.mp4"
        recorder = ClipRecorder(
            self.camera_cfg.ffmpeg,
            clip_url,
            self.service.clip_dir / name,
            self.service.cfg.animals.cat_clip_max_seconds,
        )
        if not recorder.start():
            self.last_cat_at = now
            self.service._store_cat_event(iso_now(), confidence, poster, "", camera=self.profile.id)
            return
        self.clip = recorder
        self.clip_started = now
        self.clip_name = name
        self.clip_poster = poster
        self.clip_conf = confidence
        self.clip_ts = iso_now()
        LOG.info("sentinel %s cat clip started: %s", self.profile.id, name)

    def _maybe_finish_clip(self, now: float) -> None:
        if self.clip is None:
            return
        if now - self.last_cat_seen > self.service.cfg.animals.cat_leave_seconds:
            self._finish_clip(now)
        elif now - self.clip_started > self.service.cfg.animals.cat_clip_max_seconds:
            self._finish_clip(now)

    def _finish_clip(self, now: float) -> None:
        recorder, self.clip = self.clip, None
        if recorder is not None:
            recorder.stop()
        name = self.clip_name
        path = self.service.clip_dir / name if name else None
        duration = now - self.clip_started
        if path is not None and path.exists() and path.stat().st_size > 4096:
            LOG.info("sentinel %s cat clip saved: %s (%.1f s)", self.profile.id, name, duration)
            self.service._store_cat_event(self.clip_ts or iso_now(), self.clip_conf, self.clip_poster, name, camera=self.profile.id)
        else:
            if path is not None:
                path.unlink(missing_ok=True)
            self.service._store_cat_event(self.clip_ts or iso_now(), self.clip_conf, self.clip_poster, "", camera=self.profile.id)
        self.last_cat_at = now
        self.clip_name = ""
        self.clip_poster = ""
        self.clip_conf = 0.0
        self.clip_ts = ""


class MotionGate:
    def __init__(self, cfg: MotionCfg) -> None:
        self.cfg = cfg
        self.prev: Optional[np.ndarray] = None
        self.last_active = 0.0
        self.last_processed = 0.0
        self.last_diff = 0.0

    def should_process(self, frame: np.ndarray, now: float) -> tuple[bool, float]:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, (320, 320), interpolation=cv2.INTER_AREA)
        if self.prev is None:
            diff = 999.0
        else:
            diff = float(cv2.absdiff(gray, self.prev).mean())
        self.prev = gray
        self.last_diff = diff
        if diff >= self.cfg.diff_threshold:
            self.last_active = now
        if diff >= self.cfg.diff_threshold or (now - self.last_processed) >= self.cfg.idle_poll_seconds:
            self.last_processed = now
            return True, diff
        return False, diff


@dataclass
class Track:
    id: int
    bbox: tuple[int, int, int, int]
    first_seen: float
    last_seen: float
    ground: float
    history: deque[tuple[float, float]] = field(default_factory=deque)
    votes: Counter[str] = field(default_factory=Counter)
    confidence: dict[str, float] = field(default_factory=dict)
    pending: Optional[str] = None
    pending_at: float = 0.0
    last_event: dict[str, float] = field(default_factory=dict)
    unknown_logged: bool = False
    gate_sent: set[str] = field(default_factory=set)
    activity_sent: bool = False

    @property
    def center_x(self) -> float:
        return (self.bbox[0] + self.bbox[2]) / 2.0

    @property
    def center_y(self) -> float:
        return (self.bbox[1] + self.bbox[3]) / 2.0


class Tracker:
    # One activity record per visit. A visit ends when the plate has not been
    # seen for this long, so a car parked overnight is logged once even if its
    # track is recreated by a stream hiccup.
    ACTIVITY_VISIT_SECONDS = 600.0

    def __init__(
        self,
        cfg: GateCfg,
        plate_cfg: PlateCfg,
        on_event,
        record_mode: Optional[Callable[[], str]] = None,
        on_unknown: Optional[Callable[[str, "Track", np.ndarray], None]] = None,
    ) -> None:
        self.cfg = cfg
        self.plate_cfg = plate_cfg
        self.on_event = on_event
        self.record_mode = record_mode
        self.on_unknown = on_unknown
        self.tracks: dict[int, Track] = {}
        self.next_id = 1
        self.last_gate_event: dict[str, float] = {}
        self.visits: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
        ax1, ay1, ax2, ay2 = a
        bx1, by1, bx2, by2 = b
        ix1, iy1 = max(ax1, bx1), max(ay1, by1)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
        inter = iw * ih
        if inter <= 0:
            return 0.0
        area_a = max(1, (ax2 - ax1) * (ay2 - ay1))
        area_b = max(1, (bx2 - bx1) * (by2 - by1))
        return inter / float(area_a + area_b - inter)

    def _match(self, box: tuple[int, int, int, int], now: float) -> Optional[Track]:
        best: Optional[Track] = None
        best_score = 0.0
        bx = (box[0] + box[2]) / 2.0
        by = (box[1] + box[3]) / 2.0
        diag = max(1.0, float(np.hypot(box[2] - box[0], box[3] - box[1])))
        for track in self.tracks.values():
            if now - track.last_seen > self.cfg.match_seconds:
                continue
            dist = float(np.hypot(bx - track.center_x, by - track.center_y))
            max_dist = 1.5 * diag
            if dist > max_dist:
                continue
            iou = self._iou(box, track.bbox)
            score = 0.75 * (1.0 - dist / max_dist) + 0.25 * iou
            if score > best_score:
                best_score = score
                best = track
        return best if best_score >= 0.15 else None

    def update(
        self,
        frame: np.ndarray,
        detections: list[tuple[tuple[int, int, int, int], Optional[str], float]],
        now: float,
    ) -> None:
        for plate, visit in list(self.visits.items()):
            if now - float(visit.get("last_seen") or 0.0) > self.ACTIVITY_VISIT_SECONDS:
                self.visits.pop(plate, None)
        used: set[int] = set()
        for box, plate, conf in detections:
            track = self._match(box, now)
            if track is None:
                track = Track(
                    id=self.next_id,
                    bbox=box,
                    first_seen=now,
                    last_seen=now,
                    ground=float(box[3]),
                )
                self.next_id += 1
                self.tracks[track.id] = track
            elif track.id in used:
                continue
            used.add(track.id)
            self._update_track(track, box, plate, conf, frame, now)

        for tid, track in list(self.tracks.items()):
            idle = now - track.last_seen
            # A car can drive off between two frames and never produce the
            # one-step crossing the gate logic needs. If it was clearly on the
            # other side of the line from where it started, log that crossing.
            if (
                track.pending is None
                and len(track.gate_sent) < 2
                and idle >= 2.5
                and track.votes
            ):
                best, count = track.votes.most_common(1)[0]
                if count >= self.cfg.votes and best and self._allowed(best):
                    crossed = self._slow_crossing(track)
                    if crossed and crossed not in track.gate_sent and self._claim_gate(best, crossed, now):
                        track.gate_sent.add(crossed)
                        LOG.info("track %s logged as %s after leaving the view", track.id, crossed)
                        self.on_event(best, crossed, track.confidence.get(best, 0.0), track, frame)
            # A confirmed plate that never crossed the gate line is still worth
            # recording: cars that pass by or stop short of the line would
            # otherwise never appear in the history.
            if (
                track.pending is None
                and not track.activity_sent
                and len(track.gate_sent) < 2
                and idle >= 2.5
                and track.votes
                and self._records_activity()
            ):
                best, count = track.votes.most_common(1)[0]
                if count >= self.cfg.votes and best and self._allowed(best) and self._log_activity(best):
                    direction = self._classify(track)
                    track.activity_sent = True
                    LOG.info("track %s logged as %s without a gate crossing", track.id, direction)
                    self.on_event(best, direction, track.confidence.get(best, 0.0), track, frame)
            if idle > self.cfg.track_max_age:
                self.tracks.pop(tid, None)

    @staticmethod
    def _classify(track: Track) -> str:
        recent = [ground for (ts, ground) in track.history if ts >= track.last_seen - 2.0]
        if len(recent) >= 2 and (max(recent) - min(recent)) <= 25:
            return "stop"
        return "pass"

    def _slow_crossing(self, track: Track) -> Optional[str]:
        """Return a direction when the track left at or past the gate line."""
        if not track.history:
            return None
        # Only the last few seconds matter: a car that drove in and reversed
        # out within one track must be judged by its final movement.
        recent = [ground for (ts, ground) in track.history if ts >= track.last_seen - 10.0]
        if not recent:
            recent = [track.history[0][1], track.ground]
        first, last = recent[0], recent[-1]
        moved = last - first
        line = self.cfg.line_y
        margin = self.cfg.margin
        # The crossing has to have happened near the gate: a car that is merely
        # far up the alley must not be read as a fresh crossing on expiry.
        crossed = (
            (first - line) * (last - line) < 0
            and abs(moved) >= margin * 2
            and abs(last - line) <= margin * 6
        )
        # A car that reverses out often disappears while its rear is still a
        # hair short of the line; treat that as leaving once it has travelled
        # at least one margin towards the line.
        left_at_line = abs(last - line) <= margin * 1.5 and abs(moved) >= margin
        if not (crossed or left_at_line):
            return None
        # Judge the direction by the final movement: in a dead-end alley a car
        # often drives in and then reverses straight back out.
        delta = recent[-1] - recent[-2] if len(recent) >= 2 else moved
        heading_down = delta > 0 if delta != 0 else moved > 0
        if heading_down:
            return "enter" if self.cfg.enter_direction == "down" else "exit"
        return "enter" if self.cfg.enter_direction == "up" else "exit"

    def _records_activity(self) -> bool:
        """Only log pass/stop records in the "all" mode."""
        try:
            mode = self.record_mode() if self.record_mode else self.cfg.record_mode
        except Exception:
            mode = "all"
        return mode != "crossing"

    def _claim_gate(self, plate: str, direction: str, now: float) -> bool:
        key = f"{plate}:{direction}"
        last = self.last_gate_event.get(key)
        if last is not None and now - last < self.cfg.dedupe_seconds:
            return False
        self.last_gate_event[key] = now
        return True

    def _touch_visit(self, plate: str, now: float) -> None:
        visit = self.visits.get(plate)
        if visit is None:
            visit = {"last_seen": now, "activity": False}
            self.visits[plate] = visit
        visit["last_seen"] = now

    def _log_activity(self, plate: str) -> bool:
        """Return True when this visit has not logged an activity record yet."""
        visit = self.visits.get(plate)
        if visit is None:
            visit = {"last_seen": 0.0, "activity": False}
            self.visits[plate] = visit
        if visit.get("activity"):
            return False
        visit["activity"] = True
        return True

    def _update_track(
        self,
        track: Track,
        box: tuple[int, int, int, int],
        plate: Optional[str],
        conf: float,
        frame: np.ndarray,
        now: float,
    ) -> None:
        prev_ground = track.ground
        track.bbox = box
        track.last_seen = now
        track.ground = float(box[3])
        track.history.append((now, track.ground))
        # Keep a longer window so a car that is only sampled every few seconds
        # can still be recognised as stationary.
        while track.history and now - track.history[0][0] > 15.0:
            track.history.popleft()

        if plate:
            track.votes[plate] += 1
            track.confidence[plate] = max(track.confidence.get(plate, 0.0), conf)
            self._touch_visit(plate, now)

        if now - track.first_seen >= self.cfg.min_track_seconds:
            high = self.cfg.line_y + self.cfg.margin
            low = self.cfg.line_y - self.cfg.margin
            # A car sampled at 2 fps can slide through the margin band in two
            # steps (580 -> 558 -> 505), so look back a few seconds instead of
            # requiring both thresholds inside one frame.
            recent = [ground for (ts, ground) in track.history if ts >= now - 10.0]
            was_high = any(ground >= high for ground in recent)
            was_low = any(ground <= low for ground in recent)
            direction = None
            if was_high and track.ground <= low:
                direction = "enter" if self.cfg.enter_direction == "up" else "exit"
            elif was_low and track.ground >= high:
                direction = "enter" if self.cfg.enter_direction == "down" else "exit"
            if direction and track.pending is None:
                already = direction in track.gate_sent and now - track.pending_at <= self.cfg.dedupe_seconds
                if not already:
                    track.pending = direction
                    track.pending_at = now
                    LOG.info("track %s crossing %s at y=%s (prev=%s)", track.id, direction, track.ground, prev_ground)

        if track.pending:
            best, count = track.votes.most_common(1)[0] if track.votes else ("", 0)
            if count >= self.cfg.votes and best:
                if self._allowed(best) and track.pending not in track.gate_sent and self._claim_gate(best, track.pending, now):
                    track.gate_sent.add(track.pending)
                    self.on_event(best, track.pending, track.confidence.get(best, 0.0), track, frame)
                track.pending = None
            elif now - track.pending_at >= self.cfg.pending_timeout:
                if not track.unknown_logged:
                    LOG.info("track %s crossed %s without a confirmed plate", track.id, track.pending)
                    track.unknown_logged = True
                    if self.on_unknown is not None:
                        self.on_unknown(track.pending, track, frame)
                track.pending = None
                track.votes.clear()

        # Cars that stop in view stay detected, so the disappear-based fallback
        # never fires for them. Record one "stop" while they are still there.
        if (
            not track.pending
            and not track.activity_sent
            and self._records_activity()
            and now - track.first_seen >= 3.0
            and len(track.votes) > 0
        ):
            dense = [ground for (ts, ground) in track.history if ts >= now - 2.5]
            wide = [ground for (ts, ground) in track.history if ts >= now - 12.0]
            dense_spread = (max(dense) - min(dense)) if len(dense) >= 3 else 10_000.0
            wide_spread = (max(wide) - min(wide)) if len(wide) >= 2 else 10_000.0
            if min(dense_spread, wide_spread) <= 20:
                best, count = track.votes.most_common(1)[0]
                if count >= self.cfg.votes and self._allowed(best) and self._log_activity(best):
                    track.activity_sent = True
                    LOG.info("track %s is parked and stays visible", track.id)
                    self.on_event(best, "stop", track.confidence.get(best, 0.0), track, frame)

    def _allowed(self, plate: str) -> bool:
        if not self.plate_cfg.pattern.fullmatch(plate):
            return False
        return not self.plate_cfg.allow or plate in self.plate_cfg.allow


class Store:
    def __init__(self, path: Path) -> None:
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                plate TEXT NOT NULL,
                direction TEXT NOT NULL,
                confidence REAL NOT NULL,
                snapshot TEXT,
                payload TEXT
            )"""
        )
        self.conn.commit()

    def add(self, ts: str, plate: str, direction: str, confidence: float, snapshot: str, payload: dict[str, Any]) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO events (ts, plate, direction, confidence, snapshot, payload) VALUES (?, ?, ?, ?, ?, ?)",
                (ts, plate, direction, confidence, snapshot, json.dumps(payload, ensure_ascii=False)),
            )
            self.conn.commit()

    def count_today(self) -> int:
        day = now_local().strftime("%Y-%m-%d")
        with self.lock:
            row = self.conn.execute("SELECT COUNT(*) FROM events WHERE ts LIKE ?", (f"{day}%",)).fetchone()
        return int(row[0]) if row else 0

    def list_events(
        self,
        limit: int = 100,
        offset: int = 0,
        plate: str = "",
        direction: str = "",
        day: str = "",
    ) -> tuple[list[dict[str, Any]], int]:
        where: list[str] = []
        params: list[Any] = []
        if plate:
            where.append("plate LIKE ?")
            params.append(f"%{plate}%")
        if direction in EVENT_DIRECTIONS:
            where.append("direction = ?")
            params.append(direction)
        if day:
            where.append("ts LIKE ?")
            params.append(f"{day}%")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self.lock:
            total_row = self.conn.execute(f"SELECT COUNT(*) FROM events{clause}", params).fetchone()
            rows = self.conn.execute(
                f"SELECT id, ts, plate, direction, confidence, snapshot, payload FROM events{clause} ORDER BY id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            payload: dict[str, Any] = {}
            try:
                payload = json.loads(row[6] or "{}")
            except Exception:
                payload = {}
            events.append(
                {
                    "id": row[0],
                    "ts": row[1],
                    "plate": row[2],
                    "direction": row[3],
                    "confidence": row[4],
                    "snapshot": row[5],
                    "snapshot_url": f"/snapshots/{row[5]}" if row[5] else "",
                    "clip": payload.get("clip") or "",
                    "clip_url": f"/clips/{payload['clip']}" if payload.get("clip") else "",
                    "test": bool(payload.get("test")),
                }
            )
        return events, int(total_row[0]) if total_row else 0

    def prune(self, days: int) -> None:
        cutoff = (now_local() - timedelta(days=days)).isoformat()
        with self.lock:
            self.conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            self.conn.commit()


class Service:
    def __init__(self, cfg: Config, dry_run: bool = False) -> None:
        self.cfg = cfg
        self.dry_run = dry_run
        self.data_dir = cfg.storage.data_dir
        self.snapshot_dir = self.data_dir / "snapshots"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.clip_dir = self.data_dir / "clips"
        self.clip_dir.mkdir(parents=True, exist_ok=True)
        self.retention_path = self.data_dir / "retention.json"
        self.retain_days = parse_retention(cfg.storage.retain_days) or 7
        try:
            saved_retention = json.loads(self.retention_path.read_text())
            self.retain_days = parse_retention(saved_retention.get("days")) or self.retain_days
        except Exception:
            pass
        self.settings_path = self.data_dir / "settings.json"
        self.record_mode = parse_record_mode(cfg.gate.record_mode)
        self.cat_conf = max(0.05, min(0.99, float(cfg.animals.cat_conf)))
        try:
            saved_settings = json.loads(self.settings_path.read_text())
            saved_mode = saved_settings.get("record_mode")
            if saved_mode:
                self.record_mode = parse_record_mode(saved_mode)
            saved_cat = saved_settings.get("cat_conf")
            if saved_cat is not None:
                self.cat_conf = max(0.05, min(0.99, float(saved_cat)))
        except Exception:
            pass
        self.store = Store(self.data_dir / "events.db")
        # Background style per confirmed plate: True = dark (commercial) plate.
        self.plate_styles: dict[str, bool] = {}
        self.integration_path = self.data_dir / "integration.json"
        self.webhook_url = ""
        try:
            integration = json.loads(self.integration_path.read_text())
            self.webhook_url = str(integration.get("webhook_url") or "")
        except Exception:
            pass
        self.roi_path = self.data_dir / "roi.json"
        try:
            roi_state = json.loads(self.roi_path.read_text())
            for cid, roi_value in roi_state.items():
                if cid in cfg.cameras:
                    cfg.cameras[cid].roi = parse_roi(roi_value)
        except Exception:
            pass
        # UI-managed cameras live in an overlay file so config.yaml stays the
        # factory baseline. Entries here were validated with ffprobe on add.
        self.cameras_overlay_path = self.data_dir / "cameras.json"
        self.verified: dict[str, bool] = {}
        self.overlay_ids: set[str] = set()
        self.tweaks: dict[str, dict[str, Any]] = {}
        try:
            overlay = json.loads(self.cameras_overlay_path.read_text())
            for entry in overlay.get("cameras") or []:
                profile = self.profile_from_payload(entry)
                if profile is not None:
                    cfg.cameras[profile.id] = profile
                    self.overlay_ids.add(profile.id)
            for cid, ok in (overlay.get("verified") or {}).items():
                self.verified[cid] = bool(ok)
            for cid, tweak in (overlay.get("tweaks") or {}).items():
                if cid in cfg.cameras and isinstance(tweak, dict):
                    self.tweaks[cid] = tweak
                    profile = cfg.cameras[cid]
                    if "line_y" in tweak:
                        profile.line_y = max(0, min(profile.camera.height - 1, safe_int(tweak.get("line_y"), profile.line_y)))
                    if str(tweak.get("enter_direction") or "").lower() in ("up", "down"):
                        profile.enter_direction = str(tweak["enter_direction"]).lower()
        except Exception:
            pass
        self.profile_id = cfg.active_camera
        state_path = self.data_dir / "active_camera.json"
        try:
            saved = json.loads(state_path.read_text())
            if saved.get("camera") in cfg.cameras:
                self.profile_id = saved["camera"]
        except Exception:
            pass
        self.profile = cfg.cameras[self.profile_id]
        self.camera_cfg = self.profile.camera
        self.gate_cfg = self._gate_for(self.profile)
        self.capture = Capture(self.camera_cfg)
        self.motion = MotionGate(cfg.motion)
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.switch_lock = threading.Lock()
        # Serialises ONNX inference between the main worker and cat sentinels
        # so they never oversubscribe the CPU cores.
        self.infer_lock = threading.Lock()
        self.sentinel_lock = threading.Lock()
        self.sentinel_stop = threading.Event()
        self.cat_sentinels: dict[str, CatSentinel] = {}
        self.status: dict[str, Any] = {
            "state": "starting",
            "camera_ok": False,
            "last_frame_at": None,
            "last_frame_age": None,
            "last_plate": None,
            "last_event": None,
            "last_seen": None,
            "last_snapshot": None,
            "last_error": None,
            "motion_diff": 0.0,
            "events_today": 0,
            "fps": self.camera_cfg.fps,
            "camera": self.profile_id,
            "camera_label": self.profile.label,
        }
        self.last_snapshot_path: Optional[Path] = None
        self.vehicle_detector = None
        self.alpr = None
        self.tracker: Optional[Tracker] = None
        self.thread: Optional[threading.Thread] = None
        self.last_prune = 0.0
        self.last_cat_at = 0.0
        self.last_cat_seen = 0.0
        self.pending_cat: Optional[tuple[tuple[int, int, int, int], float]] = None
        self.clip: Optional[ClipRecorder] = None
        self.clip_started = 0.0
        self.clip_name = ""
        self.clip_poster = ""
        self.clip_conf = 0.0
        self.clip_ts = ""

    def start(self) -> None:
        self._load_models()
        self.tracker = Tracker(self.gate_cfg, self.cfg.plates, self._on_event, lambda: self.record_mode, self._on_unknown)
        self.capture.start()
        self.thread = threading.Thread(target=self._run, name="alpr-worker", daemon=True)
        self.thread.start()
        self.sync_sentinels()
        with self.lock:
            self.status["state"] = "running"
        LOG.info(
            "service started camera=%s line_y=%s allow=%s webhook=%s",
            self.camera_cfg.name,
            self.gate_cfg.line_y,
            sorted(self.cfg.plates.allow) or "all",
            "configured" if self.cfg.ha.webhook_url and not self.dry_run else "dry-run",
        )

    def _gate_for(self, profile: CameraProfile) -> GateCfg:
        base = self.cfg.gate
        line_y = profile.line_y
        if profile.roi:
            rx, ry, rw, rh = profile.roi
            crop_y = int(ry * profile.camera.height)
            crop_h = max(32, int(rh * profile.camera.height))
            line_y = max(0, min(crop_h - 1, line_y - crop_y))
        return GateCfg(
            line_y=line_y,
            margin=profile.margin,
            min_track_seconds=base.min_track_seconds,
            dedupe_seconds=base.dedupe_seconds,
            votes=base.votes,
            pending_timeout=base.pending_timeout,
            track_max_age=base.track_max_age,
            enter_direction=profile.enter_direction,
            record_mode=base.record_mode,
            match_seconds=base.match_seconds,
        )

    def cameras_info(self) -> dict[str, Any]:
        overlay_ids = self.overlay_ids
        with self.sentinel_lock:
            sentinel_ids = set(self.cat_sentinels)
        return {
            "active": self.profile_id,
            "cameras": [
                {
                    "id": profile.id,
                    "label": profile.label,
                    "live_url": profile.live_url,
                    "line_y": profile.line_y,
                    "enter_direction": profile.enter_direction,
                    "roi": list(profile.roi) if profile.roi else None,
                    "width": profile.camera.width,
                    "height": profile.camera.height,
                    "managed": profile.id in overlay_ids,
                    "cat_watch": bool(profile.cat_watch),
                    "cat_active": profile.id in sentinel_ids or profile.id == self.profile_id,
                }
                for profile in self.cfg.cameras.values()
            ],
        }

    def sync_sentinels(self) -> None:
        """Start/stop cat sentinels to match per-camera cat_watch flags."""
        if self.sentinel_stop.is_set():
            return
        wanted: list[CameraProfile] = []
        for profile in self.cfg.cameras.values():
            if profile.cat_watch and profile.id != self.profile_id:
                wanted.append(profile)
        with self.sentinel_lock:
            current = set(self.cat_sentinels)
            target = {profile.id for profile in wanted}
            for cid in current - target:
                sentinel = self.cat_sentinels.pop(cid)
                threading.Thread(target=sentinel.stop, name=f"cat-stop-{cid}", daemon=True).start()
            for profile in wanted:
                if profile.id in current:
                    continue
                sentinel = CatSentinel(self, profile)
                self.cat_sentinels[profile.id] = sentinel
                threading.Thread(target=sentinel.start, name=f"cat-start-{profile.id}", daemon=True).start()
        LOG.info("cat sentinels: %s", sorted(target) or "none")

    def profile_from_payload(self, entry: dict[str, Any]) -> Optional[CameraProfile]:
        """Build a CameraProfile from a UI payload; returns None when invalid."""
        stream = parse_stream_name(entry.get("stream"))
        cid = parse_stream_name(entry.get("id") or stream)
        if not cid:
            return None
        rtsp_url = str(entry.get("rtsp_url") or "").strip()
        if not rtsp_url.startswith("rtsp://"):
            rtsp_url = f"{self.cfg.go2rtc_rtsp_base}/{stream}" if stream else ""
        if not rtsp_url.startswith("rtsp://"):
            return None
        width = safe_int(entry.get("width"), self.cfg.camera.width)
        height = safe_int(entry.get("height"), self.cfg.camera.height)
        if width < 320 or height < 240:
            return None
        live_stream = str(entry.get("live_stream") or "").strip() or parse_stream_name(entry.get("live_stream"))
        if not live_stream:
            live_stream = stream or cid
        live_base = self.cfg.go2rtc_api_url or f"http://127.0.0.1:1984"
        enter = str(entry.get("enter_direction") or "down").lower()
        if enter not in ("up", "down"):
            enter = "down"
        camera = CameraCfg(
            name=cid,
            rtsp_url=rtsp_url,
            width=width,
            height=height,
            fps=max(0.5, safe_float(entry.get("fps"), 2.0)),
            ffmpeg=str(self.cfg.camera.ffmpeg),
            startup_timeout=self.cfg.camera.startup_timeout,
            read_timeout=self.cfg.camera.read_timeout,
        )
        return CameraProfile(
            id=cid,
            label=str(entry.get("label") or cid)[:40],
            camera=camera,
            line_y=max(0, min(height - 1, safe_int(entry.get("line_y"), int(height * 0.79)))),
            margin=max(5, safe_int(entry.get("margin"), 25)),
            enter_direction=enter,
            live_url=f"{live_base}/stream.html?src={urllib.parse.quote(live_stream)}",
            roi=parse_roi(entry.get("roi")),
            clip_url=str(entry.get("clip_url") or "").strip() or rtsp_url,
            cat_watch=bool(entry.get("cat_watch", False)),
        )

    def _save_cameras_overlay(self) -> None:
        entries = []
        for profile in self.cfg.cameras.values():
            if profile.id not in self.overlay_ids:
                continue
            entries.append(
                {
                    "id": profile.id,
                    "label": profile.label,
                    "rtsp_url": profile.camera.rtsp_url,
                    "live_stream": urllib.parse.parse_qs(urllib.parse.urlparse(profile.live_url).query).get("src", [profile.id])[0],
                    "width": profile.camera.width,
                    "height": profile.camera.height,
                    "fps": profile.camera.fps,
                    "line_y": profile.line_y,
                    "enter_direction": profile.enter_direction,
                    "roi": list(profile.roi) if profile.roi else None,
                    "cat_watch": profile.cat_watch,
                }
            )
        payload = {"cameras": entries, "verified": self.verified, "tweaks": self.tweaks}
        tmp = self.cameras_overlay_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        tmp.replace(self.cameras_overlay_path)

    def add_camera(self, payload: dict[str, Any]) -> tuple[Optional[CameraProfile], str]:
        """Validate with ffprobe, then register and persist a new camera."""
        stream = parse_stream_name(payload.get("stream"))
        if not stream:
            return None, "名稱不合法（僅限英數字、點、底線、連字號）"
        if stream in self.cfg.cameras:
            return None, f"{stream} 已在監控清單中"
        probe = payload.get("probe") or {}
        width = safe_int(probe.get("width"), 0)
        height = safe_int(probe.get("height"), 0)
        if not probe.get("ok") or width < 320 or height < 240:
            return None, "請先通過連線測試再加入"
        profile = self.profile_from_payload(
            {
                "id": stream,
                "stream": stream,
                "label": payload.get("label") or stream,
                "width": width,
                "height": height,
                "fps": safe_float(probe.get("fps"), 2.0),
                "enter_direction": payload.get("enter_direction") or "down",
                "line_y": safe_int(payload.get("line_y"), int(height * 0.79)),
            }
        )
        if profile is None:
            return None, "設定不完整"
        self.cfg.cameras[profile.id] = profile
        self.overlay_ids.add(profile.id)
        self.verified[profile.id] = True
        self._save_cameras_overlay()
        LOG.info("camera %s added via UI (%sx%s)", profile.id, width, height)
        return profile, ""

    def remove_camera(self, camera_id: str) -> tuple[bool, str]:
        if camera_id not in self.cfg.cameras:
            return False, "unknown camera"
        if camera_id == self.profile_id:
            return False, "無法移除使用中鏡頭"
        if camera_id not in self.overlay_ids:
            return False, "config.yaml 內建的鏡頭請直接編輯設定檔"
        profile = self.cfg.cameras.pop(camera_id)
        self.verified.pop(camera_id, None)
        self.tweaks.pop(camera_id, None)
        try:
            state = json.loads(self.roi_path.read_text()) if self.roi_path.exists() else {}
            state.pop(camera_id, None)
            self.roi_path.write_text(json.dumps(state))
        except Exception:
            pass
        self._save_cameras_overlay()
        self.sync_sentinels()
        LOG.info("camera %s removed via UI", camera_id)
        return True, ""

    def go2rtc_streams(self) -> list[dict[str, Any]]:
        """Proxy go2rtc /api/streams and hide credentials."""
        url = f"{self.cfg.go2rtc_api_url}/api/streams"
        req = urllib.request.Request(url, headers={"User-Agent": "alpr-123"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items: list[dict[str, Any]] = []
        for name, info in (data or {}).items():
            producers = (info or {}).get("producers") or []
            source = strip_credentials(str((producers[0] if producers else {}).get("url") or ""))
            items.append(
                {
                    "name": name,
                    "source": source,
                    "h264": name.endswith("_h264") or "h264" in source,
                    "configured": name in self.cfg.cameras,
                }
            )
        items.sort(key=lambda item: (item["configured"], item["name"]))
        return items

    def test_stream(self, stream: str) -> dict[str, Any]:
        name = parse_stream_name(stream)
        if not name:
            return {"ok": False, "error": "名稱不合法"}
        rtsp_url = f"{self.cfg.go2rtc_rtsp_base}/{name}"
        user, password = rtsp_auth(self.camera_cfg.rtsp_url)
        ffprobe = str(Path(self.camera_cfg.ffmpeg).with_name("ffprobe"))
        if user:
            rtsp_url = f"rtsp://{urllib.parse.quote(user, safe='')}:{urllib.parse.quote(password, safe='')}@{rtsp_url[len('rtsp://'):]}"
        result = probe_stream(ffprobe, rtsp_url)
        if not result.get("ok"):
            # Without credentials go2rtc may still serve the stream locally.
            bare = probe_stream(ffprobe, f"{self.cfg.go2rtc_rtsp_base}/{name}")
            if bare.get("ok"):
                return bare
        return result

    def switch_camera(self, camera_id: str) -> bool:
        if camera_id not in self.cfg.cameras:
            return False
        with self.switch_lock:
            if camera_id == self.profile_id:
                return True
            LOG.info("switching camera %s -> %s", self.profile_id, camera_id)
            self.stop_event.set()
            self.capture.stop()
            if self.thread and self.thread.is_alive():
                self.thread.join(timeout=6)
            self.stop_event.clear()
            self.profile_id = camera_id
            self.profile = self.cfg.cameras[camera_id]
            self.camera_cfg = self.profile.camera
            self.gate_cfg = self._gate_for(self.profile)
            self.capture = Capture(self.camera_cfg)
            self.motion = MotionGate(self.cfg.motion)
            self.tracker = Tracker(self.gate_cfg, self.cfg.plates, self._on_event, lambda: self.record_mode, self._on_unknown)
            with self.lock:
                self.status.update(
                    {
                        "camera": camera_id,
                        "camera_label": self.profile.label,
                        "camera_ok": False,
                        "last_frame_at": None,
                        "last_error": None,
                        "fps": self.camera_cfg.fps,
                        "state": "running",
                    }
                )
            self.capture.start()
            self.thread = threading.Thread(target=self._run, name="alpr-worker", daemon=True)
            self.thread.start()
            self.sync_sentinels()
            try:
                (self.data_dir / "active_camera.json").write_text(json.dumps({"camera": camera_id}))
            except Exception as exc:
                LOG.warning("could not persist active camera: %s", exc)
            return True

    def set_roi(self, camera_id: str, roi: Any) -> bool:
        if camera_id not in self.cfg.cameras:
            return False
        parsed = parse_roi(roi) if roi else None
        with self.switch_lock:
            profile = self.cfg.cameras[camera_id]
            profile.roi = parsed
            try:
                state: dict[str, Any] = {}
                if self.roi_path.exists():
                    state = json.loads(self.roi_path.read_text())
                state[camera_id] = list(parsed) if parsed else None
                self.roi_path.write_text(json.dumps(state))
            except Exception as exc:
                LOG.warning("could not persist roi: %s", exc)
            if camera_id == self.profile_id:
                self.profile = profile
                self.gate_cfg = self._gate_for(profile)
                self.motion = MotionGate(self.cfg.motion)
                self.tracker = Tracker(self.gate_cfg, self.cfg.plates, self._on_event, lambda: self.record_mode, self._on_unknown)
        LOG.info("roi for %s set to %s", camera_id, parsed)
        return True

    def set_gate(self, camera_id: str, line_y: Optional[int], enter_direction: Optional[str]) -> bool:
        """Persist the gate line position and direction for a camera."""
        if camera_id not in self.cfg.cameras:
            return False
        profile = self.cfg.cameras[camera_id]
        with self.switch_lock:
            if line_y is not None:
                profile.line_y = max(0, min(profile.camera.height - 1, safe_int(line_y, profile.line_y)))
            if str(enter_direction or "").lower() in ("up", "down"):
                profile.enter_direction = str(enter_direction).lower()
            tweak = self.tweaks.setdefault(camera_id, {})
            tweak["line_y"] = profile.line_y
            tweak["enter_direction"] = profile.enter_direction
            self._save_cameras_overlay()
            if camera_id == self.profile_id:
                self.profile = profile
                self.gate_cfg = self._gate_for(profile)
                self.motion = MotionGate(self.cfg.motion)
                self.tracker = Tracker(self.gate_cfg, self.cfg.plates, self._on_event, lambda: self.record_mode, self._on_unknown)
        LOG.info("gate for %s line_y=%s enter=%s", camera_id, profile.line_y, profile.enter_direction)
        return True

    def set_cat_watch(self, camera_id: str, enabled: Any) -> bool:
        """Toggle the cat sentry flag for one camera and reconcile watchers."""
        if camera_id not in self.cfg.cameras:
            return False
        profile = self.cfg.cameras[camera_id]
        profile.cat_watch = bool(enabled)
        self._save_cameras_overlay()
        self.sync_sentinels()
        LOG.info("cat watch for %s set to %s", camera_id, profile.cat_watch)
        return True

    def _crop_frame(self, frame: np.ndarray) -> np.ndarray:
        roi = self.profile.roi
        if not roi:
            return frame
        height, width = frame.shape[:2]
        x = max(0, min(width - 32, int(roi[0] * width)))
        y = max(0, min(height - 32, int(roi[1] * height)))
        w = max(32, min(width - x, int(roi[2] * width)))
        h = max(32, min(height - y, int(roi[3] * height)))
        return frame[y:y + h, x:x + w]

    def _load_models(self) -> None:
        threads = self.cfg.models.threads
        vehicle_opts = ort.SessionOptions()
        vehicle_opts.intra_op_num_threads = threads
        vehicle_opts.inter_op_num_threads = 1
        vehicle_opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        plate_opts = ort.SessionOptions()
        plate_opts.intra_op_num_threads = threads
        plate_opts.inter_op_num_threads = 1

        LOG.info("loading vehicle detector %s", self.cfg.models.vehicle)
        self.vehicle_detector = create_detector(
            self.cfg.models.vehicle,
            conf_thresh=self.cfg.models.vehicle_conf,
            sess_options=vehicle_opts,
        )
        LOG.info("loading plate detector/OCR")
        plate_providers = ["CPUExecutionProvider"] if self.cfg.models.disable_coreml else None
        self.alpr = ALPR(
            detector_model=self.cfg.models.plate_detector,
            ocr_model=self.cfg.models.plate_ocr,
            detector_sess_options=plate_opts,
            ocr_sess_options=plate_opts,
            detector_providers=plate_providers,
            ocr_providers=plate_providers,
        )
        # Warm-up catches model/provider failures before the first real frame.
        blank = np.zeros((384, 384, 3), dtype=np.uint8)
        self.vehicle_detector.predict(blank)
        self.alpr.predict(blank)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                frame = self.capture.read(self.camera_cfg.startup_timeout if not self.status["camera_ok"] else self.camera_cfg.read_timeout)
                if frame is None:
                    raise RuntimeError("no frame from ffmpeg")
                frame = self._crop_frame(frame)
                now = time.monotonic()
                with self.lock:
                    self.status["camera_ok"] = True
                    self.status["last_frame_at"] = iso_now()
                    self.status["last_error"] = None
                should, diff = self.motion.should_process(frame, now)
                with self.lock:
                    self.status["motion_diff"] = round(diff, 2)
                if should:
                    self._process(frame, now)
                self._maybe_finish_clip(now)
                if now - self.last_prune > 3600:
                    self.last_prune = now
                    self._prune()
            except Exception as exc:
                LOG.exception("worker error: %s", exc)
                with self.lock:
                    self.status["camera_ok"] = False
                    self.status["last_error"] = str(exc)[:300]
                self.capture.stop()
                self.stop_event.wait(4.0)
                if not self.stop_event.is_set():
                    try:
                        self.capture.start()
                    except Exception as start_exc:
                        LOG.exception("capture restart failed: %s", start_exc)
                        self.stop_event.wait(5.0)

    def _process(self, frame: np.ndarray, now: float) -> None:
        if self.tracker is None:
            self.tracker = Tracker(self.gate_cfg, self.cfg.plates, self._on_event, lambda: self.record_mode, self._on_unknown)
        detections = self.vehicle_detector.predict(frame)
        vehicles: list[tuple[float, tuple[int, int, int, int], Any]] = []
        height, width = frame.shape[:2]
        for det in detections:
            label = str(det.label).lower()
            if label not in VEHICLE_LABELS:
                continue
            box = det.bounding_box
            x1 = max(0, int(box.x1))
            y1 = max(0, int(box.y1))
            x2 = min(width, int(box.x2))
            y2 = min(height, int(box.y2))
            if x2 - x1 < 40 or y2 - y1 < 40:
                continue
            area = float((x2 - x1) * (y2 - y1))
            vehicles.append((area, (x1, y1, x2, y2), det))
        vehicles.sort(reverse=True, key=lambda item: item[0])

        if self.cfg.animals.cat:
            cats: list[tuple[float, tuple[int, int, int, int], float]] = []
            for det in detections:
                if str(det.label).lower() != "cat":
                    continue
                conf = float(det.confidence)
                if conf < self.cat_conf:
                    continue
                box = det.bounding_box
                x1 = max(0, int(box.x1))
                y1 = max(0, int(box.y1))
                x2 = min(width, int(box.x2))
                y2 = min(height, int(box.y2))
                if x2 - x1 < 30 or y2 - y1 < 30:
                    continue
                cats.append((conf, (x1, y1, x2, y2), conf))
            if cats:
                cats.sort(reverse=True, key=lambda item: item[0])
                conf, box, _ = cats[0]
                if cat_confirmed(self, box, now):
                    self.last_cat_seen = now
                    if self.clip is None and now - self.last_cat_at >= self.cfg.animals.cat_dedupe_seconds:
                        self._start_cat_clip(frame, box, conf, now)
            else:
                self.pending_cat = None

        results: list[tuple[tuple[int, int, int, int], Optional[str], float]] = []
        for _, (x1, y1, x2, y2), _det in vehicles[: self.cfg.models.max_vehicles]:
            pad_x = int((x2 - x1) * 0.08)
            pad_y = int((y2 - y1) * 0.08)
            crop = frame[max(0, y1 - pad_y): min(height, y2 + pad_y), max(0, x1 - pad_x): min(width, x2 + pad_x)]
            if crop.size == 0:
                continue
            plate, conf = self._read_plate(crop)
            results.append(((x1, y1, x2, y2), plate, conf))

        self.tracker.update(frame, results, now)

    def _read_plate(self, crop: np.ndarray) -> tuple[Optional[str], float]:
        try:
            plates = self.alpr.predict(crop)
        except Exception as exc:
            LOG.warning("plate inference failed: %s", exc)
            return None, 0.0
        best_plate: Optional[str] = None
        best_conf = 0.0
        best_dark = False
        for result in plates:
            ocr = result.ocr
            if not ocr or not ocr.text:
                continue
            det = getattr(result, "detection", None)
            if det is not None:
                box = det.bounding_box
                plate_w = float(box.x2 - box.x1)
                plate_h = float(box.y2 - box.y1)
                if plate_h <= 0 or plate_w < self.cfg.models.min_plate_px:
                    continue
                if not (1.4 <= plate_w / plate_h <= 4.5):
                    continue
            plate = normalize_plate(ocr.text)
            confidences = ocr.confidence or []
            conf = float(min(confidences)) if confidences else 0.0
            if conf < self.cfg.models.min_ocr:
                continue
            if not self.cfg.plates.pattern.fullmatch(plate):
                continue
            if conf > best_conf:
                best_plate, best_conf = plate, conf
                best_dark = det is not None and plate_is_dark(crop, box)
        if best_plate and best_plate not in self.cfg.plates.allow and self.cfg.plates.allow:
            LOG.info("observed non-allowlisted plate %s (%.3f)", best_plate, best_conf)
        if best_plate:
            LOG.info("plate candidate %s conf=%.3f", best_plate, best_conf)
            self.plate_styles[best_plate] = best_dark
        return best_plate, best_conf

    def _on_event(self, plate: str, direction: str, confidence: float, track: Track, frame: np.ndarray) -> None:
        seen_at = iso_now()
        snapshot = self._save_snapshot(frame, track, plate, direction, confidence)
        snapshot_url = ""
        if snapshot and self.cfg.ha.public_base_url:
            snapshot_url = f"{self.cfg.ha.public_base_url}/snapshots/{snapshot.name}"
        payload = {
            "event": direction,
            "plate": plate,
            "confidence": round(confidence, 4),
            "camera": self.camera_cfg.name,
            "snapshot": snapshot_url,
            "seen_at": seen_at,
            "track_id": track.id,
        }
        with self.lock:
            self.status["last_plate"] = plate
            self.status["last_event"] = direction
            self.status["last_seen"] = seen_at
            self.status["last_snapshot"] = snapshot_url or None
            self.status["events_today"] = self.store.count_today() + 1
        self.store.add(seen_at, plate, direction, confidence, snapshot.name if snapshot else "", payload)
        LOG.info("event %s %s conf=%.3f track=%s", direction, plate, confidence, track.id)
        self._post_webhook(payload)

    def _on_unknown(self, direction: str, track: Track, frame: np.ndarray) -> None:
        """Record a gate crossing whose plate was never confirmed."""
        seen_at = iso_now()
        snapshot = self._save_snapshot(frame, track, "UNKNOWN", direction, 0.0, card=False)
        snapshot_url = ""
        if snapshot and self.cfg.ha.public_base_url:
            snapshot_url = f"{self.cfg.ha.public_base_url}/snapshots/{snapshot.name}"
        payload = {
            "event": "unknown",
            "plate": "UNKNOWN",
            "crossing": direction,
            "confidence": 0.0,
            "camera": self.camera_cfg.name,
            "snapshot": snapshot_url,
            "seen_at": seen_at,
            "track_id": track.id,
        }
        with self.lock:
            self.status["last_event"] = "unknown"
            self.status["last_seen"] = seen_at
            self.status["last_snapshot"] = snapshot_url or None
        self.store.add(seen_at, "UNKNOWN", "unknown", 0.0, snapshot.name if snapshot else "", payload)
        LOG.info("event unknown %s track=%s", direction, track.id)
        self._post_webhook(payload)

    def _annotate_cat(self, frame: np.ndarray, box: tuple[int, int, int, int], confidence: float) -> np.ndarray:
        image = frame.copy()
        x1, y1, x2, y2 = box
        colour = (0, 165, 255)
        cv2.rectangle(image, (x1, y1), (x2, y2), colour, 3)
        cv2.putText(
            image,
            f"CAT {confidence:.2f}",
            (max(8, x1), max(28, y1 - 10)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.9,
            colour,
            2,
        )
        cv2.line(image, (0, self.gate_cfg.line_y), (image.shape[1], self.gate_cfg.line_y), (80, 80, 255), 2)
        return image

    def _start_cat_clip(
        self,
        frame: np.ndarray,
        box: tuple[int, int, int, int],
        confidence: float,
        now: float,
    ) -> None:
        """Start recording when a cat enters; stop when it leaves."""
        ts = now_local().strftime("%Y%m%d-%H%M%S")
        poster = f"{ts}-{self.profile.id}-cat-{int(round(confidence * 100)):03d}.jpg"
        try:
            path = self.snapshot_dir / poster
            cv2.imwrite(str(path), self._annotate_cat(frame, box, confidence), [cv2.IMWRITE_JPEG_QUALITY, 86])
            self.last_snapshot_path = path
        except Exception as exc:
            LOG.warning("cat poster failed: %s", exc)
            poster = ""

        clip_url = (self.profile.clip_url or "").strip()
        if not clip_url:
            self.last_cat_at = now
            LOG.info("cat seen; no clip_url configured, recording a still only")
            self._store_cat_event(iso_now(), confidence, poster, "")
            return

        name = f"{ts}-cat.mp4"
        recorder = ClipRecorder(
            self.camera_cfg.ffmpeg,
            clip_url,
            self.clip_dir / name,
            self.cfg.animals.cat_clip_max_seconds,
        )
        if not recorder.start():
            self.last_cat_at = now
            self._store_cat_event(iso_now(), confidence, poster, "")
            return
        self.clip = recorder
        self.clip_started = now
        self.clip_name = name
        self.clip_poster = poster
        self.clip_conf = confidence
        self.clip_ts = iso_now()
        LOG.info("cat clip started: %s", name)

    def _maybe_finish_clip(self, now: float) -> None:
        if self.clip is None:
            return
        if now - self.last_cat_seen > self.cfg.animals.cat_leave_seconds:
            self._finish_clip(now)
        elif now - self.clip_started > self.cfg.animals.cat_clip_max_seconds:
            self._finish_clip(now)

    def _finish_clip(self, now: float) -> None:
        recorder, self.clip = self.clip, None
        if recorder is not None:
            recorder.stop()
        name = self.clip_name
        path = self.clip_dir / name if name else None
        duration = now - self.clip_started
        if path is not None and path.exists() and path.stat().st_size > 4096:
            LOG.info("cat clip saved: %s (%.1f s)", name, duration)
            self._store_cat_event(self.clip_ts or iso_now(), self.clip_conf, self.clip_poster, name)
        else:
            if path is not None:
                path.unlink(missing_ok=True)
            LOG.warning("cat clip produced no usable file; keeping the still only")
            self._store_cat_event(self.clip_ts or iso_now(), self.clip_conf, self.clip_poster, "")
        self.last_cat_at = now
        self.clip_name = ""
        self.clip_poster = ""
        self.clip_conf = 0.0
        self.clip_ts = ""

    def _store_cat_event(self, seen_at: str, confidence: float, poster: str, clip: str, camera: Optional[str] = None) -> None:
        snapshot_url = ""
        if poster and self.cfg.ha.public_base_url:
            snapshot_url = f"{self.cfg.ha.public_base_url}/snapshots/{poster}"
        clip_url = ""
        if clip and self.cfg.ha.public_base_url:
            clip_url = f"{self.cfg.ha.public_base_url}/clips/{clip}"
        payload = {
            "event": "cat",
            "plate": "CAT",
            "confidence": round(confidence, 4),
            "camera": camera or self.camera_cfg.name,
            "snapshot": snapshot_url,
            "clip": clip,
            "clip_url": clip_url,
            "seen_at": seen_at,
        }
        with self.lock:
            self.status["last_event"] = "cat"
            self.status["last_seen"] = seen_at
            self.status["last_snapshot"] = snapshot_url or None
        self.store.add(seen_at, "CAT", "cat", confidence, poster, payload)
        LOG.info("event cat conf=%.3f clip=%s", confidence, clip or "none")
        self._post_webhook(payload)

    def _save_snapshot(
        self,
        frame: np.ndarray,
        track: Track,
        plate: str,
        direction: str,
        confidence: float,
        card: bool = True,
    ) -> Optional[Path]:
        try:
            ts = now_local().strftime("%Y%m%d-%H%M%S")
            name = f"{ts}-{track.id}-{plate}-{direction}.jpg"
            path = self.snapshot_dir / name
            image = frame.copy()
            x1, y1, x2, y2 = track.bbox
            color = {
                "enter": (0, 200, 0),
                "exit": (0, 140, 255),
                "pass": (0, 180, 255),
                "stop": (200, 140, 255),
            }.get(direction, (0, 140, 255))
            cv2.rectangle(image, (x1, y1), (x2, y2), color, 3)
            label = f"{format_plate_display(plate)} {direction} {confidence:.2f}"
            cv2.putText(image, label, (max(8, x1), max(28, y1 - 10)), cv2.FONT_HERSHEY_SIMPLEX, 0.9, color, 2)
            cv2.line(image, (0, self.gate_cfg.line_y), (image.shape[1], self.gate_cfg.line_y), (80, 80, 255), 2)
            if card:
                dark = bool(self.plate_styles.get(normalize_plate(plate), False))
                image = draw_plate_card(image, format_plate_display(plate), dark=dark)
            cv2.imwrite(str(path), image, [cv2.IMWRITE_JPEG_QUALITY, 86])
            self.last_snapshot_path = path
            return path
        except Exception as exc:
            LOG.warning("snapshot failed: %s", exc)
            return None

    def _post_webhook(self, payload: dict[str, Any]) -> None:
        webhook_url = self.webhook_url or self.cfg.ha.webhook_url
        if self.dry_run or not webhook_url:
            LOG.info("webhook dry-run: %s", json.dumps(payload, ensure_ascii=False))
            return
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last_error = ""
        for attempt in range(1, self.cfg.ha.retries + 1):
            try:
                req = urllib.request.Request(
                    webhook_url,
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=self.cfg.ha.timeout) as resp:
                    if 200 <= resp.status < 300:
                        LOG.info("webhook delivered (%s) attempt=%s", resp.status, attempt)
                        return
                    last_error = f"HTTP {resp.status}"
            except urllib.error.HTTPError as exc:
                last_error = f"HTTP {exc.code}"
            except Exception as exc:
                last_error = str(exc)
            LOG.warning("webhook attempt %s failed: %s", attempt, last_error)
            time.sleep(0.5 * attempt)
        LOG.error("webhook failed after %s attempts: %s", self.cfg.ha.retries, last_error)

    def set_webhook_url(self, url: str) -> None:
        self.webhook_url = url.strip()
        try:
            self.integration_path.write_text(json.dumps({"webhook_url": self.webhook_url}))
        except Exception as exc:
            LOG.warning("could not persist integration webhook: %s", exc)

    def set_retention(self, days: Any) -> int:
        value = parse_retention(days)
        if value is None:
            raise ValueError("retention must be between 1 and 365 days")
        self.retain_days = value
        try:
            self.retention_path.write_text(json.dumps({"days": value}))
        except Exception as exc:
            LOG.warning("could not persist retention: %s", exc)
        LOG.info("snapshot retention set to %s days", value)
        self._prune()
        return value

    def set_record_mode(self, mode: Any) -> str:
        value = str(mode or "").strip().lower()
        if value not in RECORD_MODES:
            raise ValueError("record mode must be one of: " + ", ".join(RECORD_MODES))
        self.record_mode = value
        try:
            state: dict[str, Any] = {}
            if self.settings_path.exists():
                state = json.loads(self.settings_path.read_text())
            state["record_mode"] = value
            self.settings_path.write_text(json.dumps(state))
        except Exception as exc:
            LOG.warning("could not persist record mode: %s", exc)
        LOG.info("event recording mode set to %s", value)
        return value

    def set_cat_conf(self, value: Any) -> float:
        try:
            conf = float(value)
        except (TypeError, ValueError):
            raise ValueError("cat confidence must be a number between 0.05 and 0.99")
        if not 0.05 <= conf <= 0.99:
            raise ValueError("cat confidence must be between 0.05 and 0.99")
        self.cat_conf = round(conf, 2)
        try:
            state: dict[str, Any] = {}
            if self.settings_path.exists():
                state = json.loads(self.settings_path.read_text())
            state["cat_conf"] = self.cat_conf
            self.settings_path.write_text(json.dumps(state))
        except Exception as exc:
            LOG.warning("could not persist cat confidence: %s", exc)
        LOG.info("cat confidence threshold set to %.2f", self.cat_conf)
        return self.cat_conf

    def _prune(self) -> None:
        try:
            cutoff = time.time() - self.retain_days * 86400
            for path in self.snapshot_dir.glob("*.jpg"):
                if path.stat().st_mtime < cutoff and path != self.last_snapshot_path:
                    path.unlink(missing_ok=True)
            for path in self.clip_dir.glob("*.mp4"):
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            self.store.prune(self.retain_days)
            LOG.info("pruned snapshots older than %s days", self.retain_days)
        except Exception as exc:
            LOG.warning("prune failed: %s", exc)

    def stop(self) -> None:
        self.stop_event.set()
        self.sentinel_stop.set()
        with self.sentinel_lock:
            sentinels = list(self.cat_sentinels.values())
            self.cat_sentinels.clear()
        for sentinel in sentinels:
            try:
                sentinel.stop()
            except Exception as exc:
                LOG.warning("cat sentinel stop failed: %s", exc)
        if self.clip is not None:
            try:
                self._finish_clip(time.monotonic())
            except Exception as exc:
                LOG.warning("could not finish cat clip: %s", exc)
        self.capture.stop()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)
        with self.lock:
            self.status["state"] = "stopped"

    def status_snapshot(self) -> dict[str, Any]:
        with self.lock:
            data = dict(self.status)
            last = data.get("last_frame_at")
            if last:
                try:
                    age = (now_local() - datetime.fromisoformat(last)).total_seconds()
                    data["last_frame_age"] = round(age, 1)
                except Exception:
                    data["last_frame_age"] = None
            data["events_today"] = self.store.count_today()
            data["retain_days"] = self.retain_days
            data["record_mode"] = self.record_mode
            data["cat_conf"] = self.cat_conf
        return data

    def healthy(self) -> bool:
        data = self.status_snapshot()
        age = data.get("last_frame_age")
        return bool(data.get("camera_ok")) and age is not None and age < 15


def create_app(service: Service) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        service.start()
        try:
            yield
        finally:
            service.stop()

    app = FastAPI(title="ALPR-123", lifespan=lifespan)
    app.state.service = service
    if ASSET_DIR.is_dir():
        app.mount("/assets", StaticFiles(directory=ASSET_DIR), name="assets")

    dashboard_path = Path(__file__).with_name("dashboard.html")
    active_profile = service.cfg.cameras[service.profile_id]
    live_url = active_profile.live_url

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        try:
            html = dashboard_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise HTTPException(status_code=500, detail="dashboard.html missing")
        profile = service.cfg.cameras[service.profile_id]
        return HTMLResponse(
            html.replace("__LIVE_URL__", profile.live_url)
            .replace("__CAMERA_ID__", profile.id)
            .replace("__CAMERA_LABEL__", profile.label)
            .replace("__RETAIN_DAYS__", str(service.retain_days))
            .replace("__RECORD_MODE__", service.record_mode)
            .replace("__CAT_CONF__", f"{service.cat_conf:.2f}")
        )

    @app.get("/favicon.ico")
    async def favicon():
        icon = ASSET_DIR / "icon-256.png"
        if not icon.is_file():
            raise HTTPException(status_code=404, detail="no icon")
        return FileResponse(icon, media_type="image/png")

    @app.get("/api/cameras")
    def cameras(request: Request):
        return request.app.state.service.cameras_info()

    # Registered before the {camera_id} routes so "test" is not read as an id.
    @app.post("/api/cameras/test")
    def test_camera(payload: dict[str, Any], request: Request):
        svc: Service = request.app.state.service
        return svc.test_stream(str(payload.get("stream") or ""))

    @app.post("/api/cameras/{camera_id}")
    def set_camera(camera_id: str, request: Request):
        svc: Service = request.app.state.service
        if request.headers.get("X-ALPR-Switch") != "1":
            raise HTTPException(status_code=400, detail="missing X-ALPR-Switch header")
        if not svc.switch_camera(camera_id):
            raise HTTPException(status_code=404, detail="unknown camera")
        return svc.cameras_info()

    @app.post("/api/cameras/{camera_id}/roi")
    def set_camera_roi(camera_id: str, payload: dict[str, Any], request: Request):
        svc: Service = request.app.state.service
        if camera_id not in svc.cfg.cameras:
            raise HTTPException(status_code=404, detail="unknown camera")
        roi = payload.get("roi", payload)
        if roi is not None and parse_roi(roi) is None:
            raise HTTPException(status_code=400, detail="invalid roi")
        svc.set_roi(camera_id, roi)
        return svc.cameras_info()

    @app.get("/api/go2rtc/streams")
    def go2rtc_streams(request: Request):
        svc: Service = request.app.state.service
        try:
            return {"streams": svc.go2rtc_streams()}
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"go2rtc 連線失敗：{exc}") from exc

    @app.post("/api/cameras")
    def add_camera(payload: dict[str, Any], request: Request):
        svc: Service = request.app.state.service
        profile, error = svc.add_camera(payload)
        if profile is None:
            raise HTTPException(status_code=400, detail=error or "add failed")
        return svc.cameras_info()

    @app.post("/api/cameras/{camera_id}/remove")
    def remove_camera(camera_id: str, request: Request):
        svc: Service = request.app.state.service
        ok, error = svc.remove_camera(camera_id)
        if not ok:
            raise HTTPException(status_code=400, detail=error)
        return svc.cameras_info()

    @app.post("/api/cameras/{camera_id}/gate")
    def set_camera_gate(camera_id: str, payload: dict[str, Any], request: Request):
        svc: Service = request.app.state.service
        if not svc.set_gate(camera_id, payload.get("line_y"), payload.get("enter_direction")):
            raise HTTPException(status_code=404, detail="unknown camera")
        return svc.cameras_info()

    @app.post("/api/cameras/{camera_id}/cat-watch")
    def set_camera_cat_watch(camera_id: str, payload: dict[str, Any], request: Request):
        svc: Service = request.app.state.service
        if not svc.set_cat_watch(camera_id, bool(payload.get("enabled"))):
            raise HTTPException(status_code=404, detail="unknown camera")
        return svc.cameras_info()

    @app.get("/api/settings")
    def get_settings(request: Request):
        svc: Service = request.app.state.service
        return {"retain_days": svc.retain_days, "record_mode": svc.record_mode, "cat_conf": svc.cat_conf}

    @app.post("/api/settings/cat-conf")
    def set_cat_conf(payload: dict[str, Any], request: Request):
        svc: Service = request.app.state.service
        try:
            value = svc.set_cat_conf(payload.get("value"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"cat_conf": value}

    @app.post("/api/settings/record-mode")
    def set_record_mode(payload: dict[str, Any], request: Request):
        svc: Service = request.app.state.service
        try:
            mode = svc.set_record_mode(payload.get("mode"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"record_mode": mode}

    @app.post("/api/settings/retention")
    def set_retention(payload: dict[str, Any], request: Request):
        svc: Service = request.app.state.service
        try:
            days = svc.set_retention(payload.get("days"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"retain_days": days}

    @app.post("/api/integration/webhook")
    def set_integration_webhook(payload: dict[str, Any], request: Request):
        svc: Service = request.app.state.service
        svc.set_webhook_url(str(payload.get("url") or ""))
        return {"ok": True, "configured": bool(svc.webhook_url)}

    @app.delete("/api/integration/webhook")
    def clear_integration_webhook(request: Request):
        svc: Service = request.app.state.service
        svc.set_webhook_url("")
        return {"ok": True, "configured": False}

    @app.get("/api/events")
    async def events(
        request: Request,
        limit: int = 100,
        offset: int = 0,
        plate: str = "",
        direction: str = "",
        date: str = "",
    ):
        svc: Service = request.app.state.service
        limit = max(1, min(200, limit))
        offset = max(0, offset)
        plate = normalize_plate(plate)[:16]
        direction = direction if direction in EVENT_DIRECTIONS else ""
        day = date if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) else ""
        rows, total = svc.store.list_events(limit=limit, offset=offset, plate=plate, direction=direction, day=day)
        return {"events": rows, "total": total, "server_time": iso_now()}

    @app.get("/health")
    async def health(request: Request):
        svc: Service = request.app.state.service
        ok = svc.healthy()
        return JSONResponse({"ok": ok, **svc.status_snapshot()}, status_code=200 if ok else 503)

    @app.get("/status")
    async def status(request: Request):
        return request.app.state.service.status_snapshot()

    @app.get("/last.jpg")
    async def last_image(request: Request):
        svc: Service = request.app.state.service
        if svc.last_snapshot_path and svc.last_snapshot_path.exists():
            return FileResponse(svc.last_snapshot_path, media_type="image/jpeg")
        raise HTTPException(status_code=404, detail="no snapshot yet")

    @app.get("/snapshots/{name}")
    async def snapshot(name: str, request: Request):
        svc: Service = request.app.state.service
        if not SNAPSHOT_RE.fullmatch(name):
            raise HTTPException(status_code=400, detail="bad name")
        path = svc.snapshot_dir / name
        if not path.is_file():
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/clips/{name}")
    async def clip(name: str, request: Request):
        svc: Service = request.app.state.service
        if not CLIP_RE.fullmatch(name):
            raise HTTPException(status_code=400, detail="bad name")
        path = svc.clip_dir / name
        if not path.is_file():
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(path, media_type="video/mp4")

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Fixed-camera ALPR gate service")
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.yaml")))
    parser.add_argument("--dry-run", action="store_true", help="do not POST to Home Assistant")
    parser.add_argument("--once", action="store_true", help="process one frame and exit")
    parser.add_argument("--test-webhook", action="store_true", help="send a synthetic enter event and exit")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    setup_logging(cfg.storage.data_dir)
    service = Service(cfg, dry_run=args.dry_run)

    if args.test_webhook:
        service._post_webhook(
            {
                "event": "enter",
                "plate": "AB1234",
                "confidence": 0.999,
                "camera": service.camera_cfg.name,
                "snapshot": "",
                "seen_at": iso_now(),
                "test": True,
            }
        )
        return 0

    if args.once:
        service._load_models()
        service.capture.start()
        try:
            frame = service.capture.read(service.camera_cfg.startup_timeout)
            if frame is None:
                LOG.error("no frame received")
                return 2
            service._process(frame, time.monotonic())
            LOG.info("once status: %s", json.dumps(service.status_snapshot(), ensure_ascii=False))
            return 0
        except Exception as exc:
            LOG.error("once failed: %s", exc)
            return 2
        finally:
            service.capture.stop()

    app = create_app(service)
    uvicorn.run(app, host=cfg.api.host, port=cfg.api.port, log_level="warning", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
