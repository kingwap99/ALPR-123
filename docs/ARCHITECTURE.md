# Architecture

```text
RTSP 2 fps
  -> optional ROI crop
  -> 320x320 motion gate
  -> RF-DETR Nano vehicle detector
  -> vehicle crop (8% margin)
  -> fast-alpr plate detector + OCR
  -> multi-frame plate vote
  -> tracked gate line
  -> SQLite + JPEG snapshot
  -> Home Assistant webhook
```

## Vehicle-first detection

The plate detector's input is 384 pixels. A plate that is only 40–60 pixels wide in a 1920×1080 frame becomes too small after letterboxing. The service therefore detects vehicles first and runs ALPR on a crop. This also keeps the expensive OCR stage limited to the number of vehicles in the frame.

## Motion gate

Each decoded frame is reduced to a 320×320 grayscale image. The mean absolute difference against the previous frame decides whether to run the full pipeline. An idle poll runs periodically so a stationary vehicle can still be observed. Without the motion gate, a 2 fps stream would run two detector passes per second even when nothing changes.

## ROI

ROI is a normalized `(x, y, width, height)` rectangle applied immediately after decoding. The motion gate, vehicle detector, tracker, and snapshots all see only the crop. The configured gate line remains in full-frame coordinates and is converted to crop coordinates at runtime.

ROI does not reduce H.264 decoding. It reduces model invocations and the amount of downstream work, and it prevents unrelated motion from waking the pipeline.

## Tracking and events

Detections are associated by center distance and IoU. A track keeps a short history of its ground point (the bottom center of the vehicle box), plate votes, confidence, and a pending crossing. A crossing requires a minimum track age and a margin around the line. A direction is assigned from `enter_direction`:

- `up`: bottom edge moving from below the line to above is `enter`.
- `down`: bottom edge moving from above the line to below is `enter`.

An event is emitted only after the same normalized plate has enough votes. `dedupe_seconds` prevents repeated events for the same plate and direction.

## Camera switching

Each camera has a profile: RTSP URL, dimensions, line, direction, live URL, and optional ROI. `switch_camera()` stops the old ffmpeg reader, joins the worker, swaps profiles, resets motion and tracking, and starts a new reader. Models stay loaded. The active camera is stored in `active_camera.json`.

ROI settings are stored in `roi.json`. Both files are deliberately excluded from Git.

## Persistence

SQLite stores `events(id, ts, plate, direction, confidence, snapshot, payload)`. Snapshots are JPEG files in the data directory. A periodic prune removes events and files older than `retain_days`.

## Home Assistant boundary

The service does not talk to the HA WebSocket API or hold a long-lived token. It posts a small JSON document to an HA webhook. The automation owns policy: which plates are allowed, which camera may open a door, whether the door contact is closed, and which entities are controlled. This keeps the recognition service replaceable and the physical action auditable in HA.
