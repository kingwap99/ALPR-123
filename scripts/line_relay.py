#!/usr/bin/env python3
"""LINE desktop relay for ALPR-123 plate watch notifications.

Runs on a LAN Mac with the LINE desktop app logged in. The ALPR service
POSTs {chat, text, image_b64} to /notify; this relay pastes the text and
snapshot into the named LINE group using the same AppleScript + Quartz
approach as shopee_watch. Requests are serialised because the automation
drives the real UI.

Usage:
    python3 line_relay.py --port 8098            # serve (needs pyobjc Quartz)
    python3 line_relay.py --selftest CHAT        # send a test message + image

Config: ~/.config/alpr-line-relay/config.json {"token": "..."} (optional).
The same token must be set in the ALPR dashboard's LINE watch settings.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOG = logging.getLogger("line-relay")
CONFIG_PATH = Path.home() / ".config" / "alpr-line-relay" / "config.json"
IMAGE_DIR = Path.home() / "Library" / "Application Support" / "alpr-line-relay" / "images"
MAX_IMAGE_BYTES = 8 * 1024 * 1024

_ui_lock = threading.Lock()
_pending_lock = threading.Lock()
_pending = 0


def load_token() -> str:
    try:
        return str(json.loads(CONFIG_PATH.read_text()).get("token") or "")
    except Exception:
        return ""


def _osascript(script: str) -> str:
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"AppleScript failed: {result.stderr.strip()}")
    return result.stdout.strip()


def line_running() -> bool:
    try:
        out = _osascript('tell application "System Events" to get name of every process whose name is "LINE"')
        return "LINE" in out
    except Exception:
        return False


def _line_window() -> tuple[int, int, int, int]:
    _osascript('tell application "LINE" to activate')
    time.sleep(1.2)
    raw = _osascript(
        'tell application "System Events" to tell process "LINE" '
        "to get {position of window 1, size of window 1}"
    )
    numbers = [int(float(value)) for value in raw.replace(",", " ").split()]
    if len(numbers) != 4:
        raise RuntimeError(f"cannot read LINE window bounds: {raw!r}")
    return numbers[0], numbers[1], numbers[2], numbers[3]


def _click(x: int, y: int) -> None:
    import Quartz

    point = Quartz.CGPointMake(x, y)
    Quartz.CGEventPost(
        Quartz.kCGHIDEventTap,
        Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, point, 0),
    )
    time.sleep(0.05)
    Quartz.CGEventPost(
        Quartz.kCGHIDEventTap,
        Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDown, point, 0),
    )
    time.sleep(0.06)
    Quartz.CGEventPost(
        Quartz.kCGHIDEventTap,
        Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp, point, 0),
    )


def _keys(key_cmd: str) -> None:
    template = """
tell application "System Events"
  tell process "LINE"
    {key_cmd}
  end tell
end tell
"""
    _osascript(template.format(key_cmd=key_cmd))


def _type_text(text: str) -> None:
    subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True, timeout=10)
    _keys('keystroke "v" using command down')


def _copy_image(path: Path) -> None:
    _osascript(f'set the clipboard to (read (POSIX file "{path}") as JPEG picture)')


def _ui_pos(expr: str) -> tuple[int, int]:
    raw = _osascript(
        'tell application "System Events" to tell process "LINE" '
        f"to get position of {expr}"
    )
    nums = [int(float(v)) for v in raw.replace(",", " ").split()]
    if len(nums) != 2:
        raise RuntimeError(f"cannot read UI position: {raw!r}")
    return nums[0], nums[1]


def _ui_size(expr: str) -> tuple[int, int]:
    raw = _osascript(
        'tell application "System Events" to tell process "LINE" '
        f"to get size of {expr}"
    )
    nums = [int(float(v)) for v in raw.replace(",", " ").split()]
    if len(nums) != 2:
        raise RuntimeError(f"cannot read UI size: {raw!r}")
    return nums[0], nums[1]


# LINE 9.x exposes the search field directly under the outer splitter
# group, while older builds nested it one level deeper. Try both.
_SEARCH_EXPRS = (
    "text field 1 of splitter group 1 of window 1",
    "text field 1 of splitter group 1 of splitter group 1 of window 1",
)
_COMPOSE_EXPRS = (
    "text area 1 of splitter group 1 of splitter group 1 of window 1",
    "text area 1 of splitter group 1 of window 1",
)


def _first_expr(exprs: tuple[str, ...]) -> str:
    last = None
    for expr in exprs:
        try:
            _ui_pos(expr)
            return expr
        except RuntimeError as exc:
            last = exc
    raise RuntimeError(f"LINE UI element not found: {last}")


def _compose_value() -> str:
    expr = _first_expr(_COMPOSE_EXPRS)
    return _osascript(
        'tell application "System Events" to tell process "LINE" to get value of '
        + expr
    )


def _search_value() -> str:
    expr = _first_expr(_SEARCH_EXPRS)
    return _osascript(
        'tell application "System Events" to tell process "LINE" to get value of '
        + expr
    )


def open_chat(chat_name: str) -> tuple[int, int, int, int]:
    """Open a chat via the search box; returns LINE window bounds."""
    wx, wy, ww, wh = _line_window()
    sx, sy = _ui_pos(_first_expr(_SEARCH_EXPRS))
    sw, sh = _ui_size(_first_expr(_SEARCH_EXPRS))
    _click(sx + sw // 2, sy + sh // 2)
    time.sleep(0.5)
    _keys('keystroke "a" using command down')
    _keys("key code 51")
    time.sleep(0.2)
    _type_text(chat_name)
    time.sleep(2.0)
    try:
        typed = _search_value()
    except RuntimeError:
        typed = ""
    if chat_name.lower() not in typed.lower():
        LOG.info("search box holds %r, retyping chat name", typed)
        _click(sx + sw // 2, sy + sh // 2)
        time.sleep(0.4)
        _keys('keystroke "a" using command down')
        _keys("key code 51")
        _type_text(chat_name)
        time.sleep(2.0)
        if chat_name.lower() not in _search_value().lower():
            raise RuntimeError("chat name did not land in the search box; aborting")
    # Click the first search result just below the search field.
    _click(sx + sw // 2, sy + sh + 34)
    time.sleep(1.5)
    return wx, wy, ww, wh


def _compose_click() -> None:
    cx, cy = _ui_pos(_first_expr(_COMPOSE_EXPRS))
    cw, ch = _ui_size(_first_expr(_COMPOSE_EXPRS))
    _click(cx + cw // 2, cy + ch // 2)


def send_to_chat(chat_name: str, text: str, image_path: Path | None = None) -> None:
    try:
        wx, wy, ww, wh = open_chat(chat_name)
    except RuntimeError as error:
        if "window" not in str(error):
            raise
        LOG.info("LINE has no window; launching and retrying")
        subprocess.run(["open", "-a", "LINE"], check=False, timeout=15)
        time.sleep(6)
        wx, wy, ww, wh = open_chat(chat_name)

    if image_path is not None:
        _compose_click()
        time.sleep(0.4)
        _copy_image(image_path)
        time.sleep(0.4)
        _keys('keystroke "v" using command down')
        time.sleep(1.2)
        _keys("key code 36")
        time.sleep(1.5)

    _compose_click()
    time.sleep(0.4)
    try:
        leftover = _compose_value()
    except RuntimeError:
        leftover = ""
    if leftover.strip():
        _keys('keystroke "a" using command down')
        _keys("key code 51")
        time.sleep(0.3)
    _type_text(text)
    time.sleep(0.8)
    marker = text.strip().splitlines()[0][:8]
    pasted = False
    for _ in range(8):
        time.sleep(0.4)
        try:
            if marker in _compose_value():
                pasted = True
                break
        except RuntimeError:
            pass
    if not pasted:
        raise RuntimeError("text did not land in the LINE compose box; aborting")
    _keys("key code 36")
    sent = False
    for _ in range(10):
        time.sleep(0.5)
        try:
            if not _compose_value().strip():
                sent = True
                break
        except RuntimeError:
            pass
    if not sent:
        raise RuntimeError("compose box did not clear after Enter; message not sent")


def prune_images(days: int = 7) -> None:
    cutoff = time.time() - days * 86400
    try:
        for path in IMAGE_DIR.glob("*.jpg"):
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
    except Exception as exc:
        LOG.warning("image prune failed: %s", exc)


def handle_notify(payload: dict) -> dict:
    global _pending
    chat = str(payload.get("chat") or "").strip()
    text = str(payload.get("text") or "").strip()
    image_b64 = str(payload.get("image_b64") or "")
    if not chat or not text:
        raise ValueError("chat and text are required")
    image_path = None
    if image_b64:
        raw = base64.b64decode(image_b64)
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError("image too large")
        IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        image_path = IMAGE_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{threading.get_ident()}.jpg"
        image_path.write_bytes(raw)
    with _pending_lock:
        _pending += 1
    try:
        with _ui_lock:
            send_to_chat(chat, text, image_path)
    finally:
        with _pending_lock:
            _pending -= 1
    LOG.info("delivered to %r (image=%s)", chat, bool(image_path))
    return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def _json(self, code: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        token = load_token()
        return not token or self.headers.get("X-Relay-Token") == token

    def do_GET(self):
        if self.path != "/health":
            self._json(404, {"detail": "not found"})
            return
        if not self._authorized():
            self._json(403, {"detail": "bad token"})
            return
        with _pending_lock:
            pending = _pending
        self._json(200, {"ok": True, "line_running": line_running(), "pending": pending})

    def do_POST(self):
        if self.path != "/notify":
            self._json(404, {"detail": "not found"})
            return
        if not self._authorized():
            self._json(403, {"detail": "bad token"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_IMAGE_BYTES * 2:
                raise ValueError("bad content length")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            result = handle_notify(payload)
            self._json(200, result)
        except ValueError as exc:
            self._json(400, {"detail": str(exc)})
        except Exception as exc:
            LOG.error("notify failed: %s", exc)
            self._json(500, {"detail": str(exc)})


def main() -> int:
    parser = argparse.ArgumentParser(description="ALPR LINE desktop relay")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8098)
    parser.add_argument("--selftest", metavar="CHAT", help="send a test message to CHAT and exit")
    parser.add_argument("--image", help="optional jpg to attach in selftest")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)

    if args.selftest:
        text = f"✅ ALPR LINE relay test\n{time.strftime('%Y-%m-%d %H:%M:%S')}"
        send_to_chat(args.selftest, text, Path(args.image) if args.image else None)
        print("selftest sent")
        return 0

    prune_images()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    LOG.info("line relay listening on %s:%s (token %s)", args.host, args.port, "set" if load_token() else "unset")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
