# HTTP API

The API listens on `api.host:api.port` (default `0.0.0.0:8099`). It has no authentication; keep it on a trusted network.

## Health

```http
GET /health
```

Returns 200 when the worker has a frame younger than 15 seconds. Returns 503 during startup or camera failure. The body includes the status fields below.

## Status

```http
GET /status
```

```json
{
  "state": "running",
  "camera_ok": true,
  "camera": "driveway",
  "camera_label": "Driveway (production)",
  "last_frame_age": 0.4,
  "last_plate": "AB1234",
  "last_event": "enter",
  "last_seen": "2026-10-22T12:00:00+08:00",
  "last_snapshot": "http://ALPR_HOST:8099/snapshots/example.jpg",
  "events_today": 1,
  "motion_diff": 0.2,
  "fps": 2.0
}
```

## Cameras

```http
GET /api/cameras
```

Returns the active camera and every profile, including `line_y`, `enter_direction`, `live_url`, and `roi`.

```http
POST /api/cameras/driveway
```

Stops the current capture and starts the named profile. The response is the updated camera list. Switching takes 10–30 seconds while the new RTSP reader waits for a keyframe.

## ROI

```http
POST /api/cameras/driveway/roi
Content-Type: application/json

{"roi": [0.10, 0.25, 0.80, 0.65]}
```

Values are normalized `x, y, width, height`. Send `{"roi": null}` to clear. The response is the updated camera list. An invalid rectangle returns 400.

## Events

```http
GET /api/events?limit=100&offset=0&plate=AB&direction=enter&date=2026-10-22
```

All filters are optional. `limit` is capped at 200. The response is:

```json
{
  "events": [
    {
      "id": 1,
      "ts": "2026-10-22T12:00:00+08:00",
      "plate": "AB1234",
      "direction": "enter",
      "confidence": 0.998,
      "snapshot": "example.jpg",
      "snapshot_url": "/snapshots/example.jpg",
      "test": false
    }
  ],
  "total": 1,
  "server_time": "2026-10-22T12:00:01+08:00"
}
```

## Images

```http
GET /snapshots/{name}.jpg
GET /last.jpg
```

`/last.jpg` is the most recent saved event. Both return 404 when no image exists.
