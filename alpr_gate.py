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
import urllib.request
from collections import Counter, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

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
VEHICLE_LABELS = {"car", "truck", "bus"}
ASSET_DIR = Path(__file__).with_name("assets")
PLATE_FONT_PATH = ASSET_DIR / "fonts" / "TWPlateSans-Bold.ttf"


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


def draw_plate_card(image: np.ndarray, plate_text: str, font_path: Path = PLATE_FONT_PATH) -> np.ndarray:
    """Draw a small reference plate using the bundled plate font."""
    if not font_path.is_file() or not plate_text:
        return image
    height, width = image.shape[:2]
    plate_w = max(160, min(360, int(width * 0.36)))
    plate_h = max(64, int(plate_w / 2.45))
    margin = max(10, int(height * 0.025))
    card = Image.new("RGBA", (plate_w, plate_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)
    radius = max(8, int(plate_h * 0.12))
    draw.rounded_rectangle((2, 2, plate_w - 3, plate_h - 3), radius=radius, fill=(247, 250, 255, 255), outline=(155, 168, 187, 255), width=max(2, plate_h // 45))
    inset = max(5, plate_h // 18)
    draw.rounded_rectangle((inset, inset, plate_w - inset - 1, plate_h - inset - 1), radius=max(4, radius - 3), outline=(214, 223, 235, 255), width=1)
    slot_w = max(12, plate_w // 16)
    slot_h = max(2, plate_h // 45)
    draw.rounded_rectangle((plate_w * 0.23 - slot_w / 2, inset + 3, plate_w * 0.23 + slot_w / 2, inset + 3 + slot_h), radius=slot_h, fill=(20, 26, 36, 255))
    draw.rounded_rectangle((plate_w * 0.77 - slot_w / 2, inset + 3, plate_w * 0.77 + slot_w / 2, inset + 3 + slot_h), radius=slot_h, fill=(20, 26, 36, 255))
    size = max(12, int(plate_h * 0.62))
    while size > 8:
        font = ImageFont.truetype(str(font_path), size)
        box = draw.textbbox((0, 0), plate_text, font=font)
        if box[2] - box[0] <= plate_w * 0.84 and box[3] - box[1] <= plate_h * 0.60:
            break
        size -= 2
    draw.text((plate_w / 2, plate_h * 0.55), plate_text, font=font, fill=(11, 19, 34, 255), anchor="mm")
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


@dataclass
class MotionCfg:
    diff_threshold: float
    idle_poll_seconds: float


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
    ha: HaCfg
    storage: StorageCfg
    api: ApiCfg
    plates: PlateCfg
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
        ),
        cameras=profiles,
        active_camera=active_camera,
        motion=MotionCfg(
            diff_threshold=max(0.2, safe_float(motion.get("diff_threshold"), 2.5)),
            idle_poll_seconds=max(2.0, safe_float(motion.get("idle_poll_seconds"), 8.0)),
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

    @property
    def center_x(self) -> float:
        return (self.bbox[0] + self.bbox[2]) / 2.0

    @property
    def center_y(self) -> float:
        return (self.bbox[1] + self.bbox[3]) / 2.0


class Tracker:
    def __init__(self, cfg: GateCfg, plate_cfg: PlateCfg, on_event) -> None:
        self.cfg = cfg
        self.plate_cfg = plate_cfg
        self.on_event = on_event
        self.tracks: dict[int, Track] = {}
        self.next_id = 1

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
            if now - track.last_seen > 1.5:
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

        expired = [tid for tid, track in self.tracks.items() if now - track.last_seen > self.cfg.track_max_age]
        for tid in expired:
            self.tracks.pop(tid, None)

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
        while track.history and now - track.history[0][0] > 6.0:
            track.history.popleft()

        if plate:
            track.votes[plate] += 1
            track.confidence[plate] = max(track.confidence.get(plate, 0.0), conf)

        if now - track.first_seen >= self.cfg.min_track_seconds:
            high = self.cfg.line_y + self.cfg.margin
            low = self.cfg.line_y - self.cfg.margin
            direction = None
            if prev_ground >= high and track.ground <= low:
                direction = "enter" if self.cfg.enter_direction == "up" else "exit"
            elif prev_ground <= low and track.ground >= high:
                direction = "enter" if self.cfg.enter_direction == "down" else "exit"
            if direction and track.pending is None:
                track.pending = direction
                track.pending_at = now
                LOG.info("track %s crossing %s at y=%s (prev=%s)", track.id, direction, track.ground, prev_ground)

        if track.pending:
            best, count = track.votes.most_common(1)[0] if track.votes else ("", 0)
            if count >= self.cfg.votes and best:
                if self._allowed(best):
                    last = track.last_event.get(track.pending)
                    if last is None or now - last >= self.cfg.dedupe_seconds:
                        track.last_event[track.pending] = now
                        self.on_event(best, track.pending, track.confidence.get(best, 0.0), track, frame)
                track.pending = None
                track.votes.clear()
            elif now - track.pending_at >= self.cfg.pending_timeout:
                if not track.unknown_logged:
                    LOG.info("track %s crossed %s without a confirmed plate", track.id, track.pending)
                    track.unknown_logged = True
                track.pending = None
                track.votes.clear()

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
        if direction in {"enter", "exit"}:
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
        self.store = Store(self.data_dir / "events.db")
        self.roi_path = self.data_dir / "roi.json"
        try:
            roi_state = json.loads(self.roi_path.read_text())
            for cid, roi_value in roi_state.items():
                if cid in cfg.cameras:
                    cfg.cameras[cid].roi = parse_roi(roi_value)
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

    def start(self) -> None:
        self._load_models()
        self.tracker = Tracker(self.gate_cfg, self.cfg.plates, self._on_event)
        self.capture.start()
        self.thread = threading.Thread(target=self._run, name="alpr-worker", daemon=True)
        self.thread.start()
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
        )

    def cameras_info(self) -> dict[str, Any]:
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
                }
                for profile in self.cfg.cameras.values()
            ],
        }

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
            self.tracker = Tracker(self.gate_cfg, self.cfg.plates, self._on_event)
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
                self.tracker = Tracker(self.gate_cfg, self.cfg.plates, self._on_event)
        LOG.info("roi for %s set to %s", camera_id, parsed)
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
        self.alpr = ALPR(
            detector_model=self.cfg.models.plate_detector,
            ocr_model=self.cfg.models.plate_ocr,
            detector_sess_options=plate_opts,
            ocr_sess_options=plate_opts,
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
            self.tracker = Tracker(self.gate_cfg, self.cfg.plates, self._on_event)
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
        for result in plates:
            ocr = result.ocr
            if not ocr or not ocr.text:
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
        if best_plate and best_plate not in self.cfg.plates.allow and self.cfg.plates.allow:
            LOG.info("observed non-allowlisted plate %s (%.3f)", best_plate, best_conf)
        if best_plate:
            LOG.info("plate candidate %s conf=%.3f", best_plate, best_conf)
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

    def _save_snapshot(
        self, frame: np.ndarray, track: Track, plate: str, direction: str, confidence: float
    ) -> Optional[Path]:
        try:
            ts = now_local().strftime("%Y%m%d-%H%M%S")
            name = f"{ts}-{track.id}-{plate}-{direction}.jpg"
            path = self.snapshot_dir / name
            image = frame.copy()
            x1, y1, x2, y2 = track.bbox
            color = (0, 200, 0) if direction == "enter" else (0, 140, 255)
            cv2.rectangle(image, (x1, y1), (x2, y2), color, 3)
            label = f"{format_plate_display(plate)} {direction} {confidence:.2f}"
            cv2.putText(image, label, (max(8, x1), max(28, y1 - 10)), cv2.FONT_HERSHEY_SIMPLEX, 0.9, color, 2)
            cv2.line(image, (0, self.gate_cfg.line_y), (image.shape[1], self.gate_cfg.line_y), (80, 80, 255), 2)
            image = draw_plate_card(image, format_plate_display(plate))
            cv2.imwrite(str(path), image, [cv2.IMWRITE_JPEG_QUALITY, 86])
            self.last_snapshot_path = path
            return path
        except Exception as exc:
            LOG.warning("snapshot failed: %s", exc)
            return None

    def _post_webhook(self, payload: dict[str, Any]) -> None:
        if self.dry_run or not self.cfg.ha.webhook_url:
            LOG.info("webhook dry-run: %s", json.dumps(payload, ensure_ascii=False))
            return
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last_error = ""
        for attempt in range(1, self.cfg.ha.retries + 1):
            try:
                req = urllib.request.Request(
                    self.cfg.ha.webhook_url,
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

    def _prune(self) -> None:
        try:
            cutoff = time.time() - self.cfg.storage.retain_days * 86400
            for path in self.snapshot_dir.glob("*.jpg"):
                if path.stat().st_mtime < cutoff and path != self.last_snapshot_path:
                    path.unlink(missing_ok=True)
            self.store.prune(self.cfg.storage.retain_days)
            LOG.info("pruned snapshots older than %s days", self.cfg.storage.retain_days)
        except Exception as exc:
            LOG.warning("prune failed: %s", exc)

    def stop(self) -> None:
        self.stop_event.set()
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

    app = FastAPI(title="ALPR Gate", lifespan=lifespan)
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
        direction = direction if direction in {"enter", "exit"} else ""
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
