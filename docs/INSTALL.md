# Installation

This guide installs ALPR-123 next to an existing `go2rtc` instance and connects it to Home Assistant. Replace `ALPR_HOST`, `GO2RTC_HOST`, `HOME_ASSISTANT_HOST`, stream names, and entity IDs with your own values.

## 1. Prepare the camera streams

Create a low-rate ALPR stream and a smooth live stream in `go2rtc`. The ALPR stream is the only one consumed continuously.

```yaml
streams:
  driveway:
    - rtsp://USER:PASSWORD@CAMERA_IP:554/stream1

  driveway_alpr:
    - ffmpeg:driveway#video=h264#hardware=videotoolbox#raw=-r 2 -g 4 -bf 0 -vf hwdownload,format=nv12,scale=1920:1080

  driveway_h264:
    - ffmpeg:driveway#video=h264#hardware=videotoolbox#raw=-r 15 -g 30 -bf 0 -vf hwdownload,format=nv12,scale=1280:720
```

On Linux, remove `hardware=videotoolbox` and the hardware-specific `hwdownload,format=nv12` filter, or adapt it to your encoder. If your camera already provides a suitable H.264 substream, use it directly instead of transcoding.

The resulting RTSP URL for the service is:

```text
rtsp://GO2RTC_USER:GO2RTC_PASSWORD@GO2RTC_HOST:8554/driveway_alpr
```

The live URL is `http://GO2RTC_HOST:1984/stream.html?src=driveway_h264`.

## 2. Install the service

### macOS

```sh
git clone https://github.com/kingwap99/ALPR-123.git
cd ALPR-123
./scripts/install_macos.sh
```

The installer creates `~/alpr-gate`, installs the Python environment, copies the example configuration only if `config.yaml` does not exist, and loads a launchd user agent. Edit the configuration before starting the service for the first time.

The repository includes `assets/icon-256.png` for the UI and `assets/fonts/TWPlateSans-Bold.ttf` for the reference plate drawn on snapshots. `Pillow` is installed from `requirements.txt`.

Manual equivalent:

```sh
mkdir -p ~/alpr-gate
cd ~/alpr-gate
/opt/homebrew/bin/python3.11 -m venv venv
venv/bin/pip install -U pip
venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml
chmod 600 config.yaml
```

### Linux

```sh
sudo mkdir -p /opt/alpr-gate
sudo chown "$USER": "$USER" /opt/alpr-gate
git clone https://github.com/kingwap99/ALPR-123.git /opt/alpr-gate
cd /opt/alpr-gate
python3 -m venv venv
venv/bin/pip install -U pip
venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml
chmod 600 config.yaml
```

Install `deploy/alpr-gate.service` under `/etc/systemd/system/`, edit its user and paths, then run:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now alpr-gate
```

## 3. Configure the service

Edit `config.yaml` and replace every placeholder:

- `camera` and `cameras.*.rtsp_url`: the ALPR stream URL, including go2rtc credentials.
- `cameras.*.live_url`: the browser-facing live stream URL.
- `active_camera`: the first camera to load.
- `cameras.*.line_y` and `enter_direction`: the gate crossing in full-frame coordinates.
- `plates.allow`: known plates, or an empty list for observe-only mode.
- `ha.webhook_url`: the HA webhook URL, or empty for dry-run mode.
- `ha.public_base_url`: the URL that HA and your phone can use to load event images.

On Linux also set `camera.ffmpeg` to `/usr/bin/ffmpeg` and `storage.data_dir` to a writable path such as `/var/lib/alpr-gate`.

Keep the file at mode `600`; it contains camera and webhook secrets.

The optional ROI is a normalized rectangle:

```yaml
roi: {x: 0.10, y: 0.25, width: 0.80, height: 0.65}
```

You can also draw it in the web UI with **Detection area**. The ROI is applied before motion and vehicle detection. It reduces model runs, but it does not reduce full-frame H.264 decoding.

## 4. First run

```sh
cd ~/alpr-gate
venv/bin/python alpr_gate.py --config config.yaml --dry-run
```

Open `http://ALPR_HOST:8099/` and check:

- The status pill says the service is healthy.
- The live camera loads.
- A known plate produces an `enter`/`exit` event when it crosses the configured line, and a `pass`/`stop` entry when it drives by or stops without crossing.
- The history card opens a full snapshot.

The first frame can take 10–30 seconds because ffmpeg waits for a keyframe. The health endpoint returns 503 until the first frame arrives.

## 5. Home Assistant native integration

Copy `custom_components/alpr_gate` into the Home Assistant `config` directory, then restart Home Assistant.

Open **Settings → Devices & Services → Add Integration → ALPR-123** and enter the ALPR host and port. The integration:

- creates sensors for last plate, last event, last seen, events today, camera, and confidence;
- creates camera/service online binary sensors;
- fires `alpr_gate_event` for automations;
- registers its webhook with the ALPR service automatically when HA has an internal URL; otherwise it polls every five seconds.

Use `examples/home_assistant/integration_automation.yaml` as the event-triggered automation. No manual webhook ID or helper setup is required for the native integration.

If you use HACS, add this repository as a custom **Integration** repository and install **ALPR-123** from HACS.

## 6. Optional webhook automation

Create a random webhook ID and add the automation from `examples/home_assistant/automation.yaml`. Replace:

- `REPLACE_WITH_RANDOM_ID`
- `AB1234` with your known plate
- `switch.your_outside_light`
- `switch.garage_door_up`
- `binary_sensor.garage_door_contact`
- `driveway` with the production camera ID

Add `input_text` and `input_datetime` helpers from `examples/home_assistant/helpers.yaml` if you want the last plate, direction, and time on a dashboard.

Set the service webhook:

```yaml
ha:
  webhook_url: "http://HOME_ASSISTANT_HOST:8123/api/webhook/REPLACE_WITH_RANDOM_ID"
```

The automation uses `local_only: true`. Keep the service and HA on the same trusted LAN.

### Add the UI to the HA sidebar

1. Settings → Dashboards → Add dashboard.
2. Choose **Webpage panel**.
3. Enter `http://ALPR_HOST:8099/`.
4. Set the title and URL path, then create it.

The iframe works on a local HTTP HA session. A remote HTTPS HA session may block an HTTP iframe; open the ALPR URL directly in that case.

## 7. Camera selection, ROI, and snapshot retention

Choose a camera in the UI; it switches immediately. The selection changes the ALPR source and the live view, is persisted in `active_camera.json`, and survives a restart. Programmatic selector updates during page load are ignored, so reloading the page does not send a switch request.

The **Camera settings** button adds new cameras without editing YAML. The panel lists every stream on the go2rtc server (credentials stripped). **Test** probes the stream with ffprobe; only a stream that answers can be **Added**. UI-added cameras are stored in `cameras.json` next to the database, survive restarts, and can be removed from the same panel without touching their history. The **Detection area** editor also drags the pink gate line and selects the enter direction, saved per camera.

Use a test camera with a known parked car to validate plate recognition and the webhook first. Switch to the production camera only when the line, direction, and ROI are correct. Opening a garage door is a physical action; test it with the door in view and a manual stop available.

The saved ROI is outlined with a dashed box on the live view, fitted to the displayed video area. Use **隱藏偵測範圍 / 顯示偵測範圍** in the live card header to toggle it; the choice is remembered in the browser.

The **記錄** selector in the header switches the recording mode. **全部經過** (default) records `enter`/`exit` crossings and also logs `pass`/`stop` when a confirmed car leaves the view without crossing. **只看跨越** restores the older behaviour where only gate crossings create events. The change applies immediately and is stored in `settings.json`; `gate.record_mode` in `config.yaml` is only the default.

In **全部經過** mode a car that stays parked in view is recorded once as `stop`, not on every poll, and driving off over the line still produces its own `enter`/`exit`. Repeated sightings of the same plate count as one visit; a new record is only possible after the plate has not been seen for 10 minutes (`Tracker.ACTIVITY_VISIT_SECONDS`). Because the motion gate only samples a still scene every 8 seconds, keep `gate.match_seconds` (default 12) at least that long; otherwise every sample becomes a new track and the vote count never reaches `gate.votes`.

Plate detections that are too small are discarded: `models.min_plate_px` (default 60 pixels) plus a 1.4-4.5 aspect-ratio check drops background patches and far-away plates that OCR happily turns into confident nonsense. Lower the value if plates in your scene are genuinely small; raise it if false reads persist.

When a car leaves between two frames it may never produce the single-step crossing the gate logic needs. On track expiry the tracker looks at the last 10 seconds: a clear move from one side of the line to the other (more than twice `margin`) records the crossing, and so does vanishing within one and a half `margin` of the line after moving at least one `margin` towards it. The direction comes from the final movement, which is what a dead-end alley needs when a car drives in and then reverses straight out.

A car that enters and reverses out inside a minute gets both `enter` and `exit`: gate dedupe is per direction, and a confirmed plate is kept after a crossing, so the way out does not depend on reading the plate again while reversing. Once both directions are recorded the car no longer adds a redundant 經過 (pass) row.

When a crossing happens but the plate never resolves (turning into a driveway, backlight, steep angle), an `unknown` event is written after `pending_timeout`: the frame is saved, the crossing direction goes into the payload as `crossing`, the history shows 未辨識, and Home Assistant receives an `event: unknown` webhook. OCR failures no longer hide the fact that a car went through.

If the service stalls at `loading plate detector/OCR` with 0% CPU on restart, CoreML is deadlocked compiling the plate model. Set `models.disable_coreml: true` to use the CPU provider; it is slower but always starts.

Cats are recorded as video. With `animals.cat` enabled, seeing a cat starts a recording from `cameras.*.clip_url` (point it at the go2rtc 15 fps stream); the H.264 stream is copied without re-encoding. Recording stops after `cat_leave_seconds` without a cat, capped by `cat_clip_max_seconds`. Each visit stores one `cat` event, an annotated still, and an MP4; the history can filter by 貓咪 and the modal plays the clip. Clips follow the same retention as snapshots.

The cat confidence threshold is adjustable from the **貓咪 ≥** selector in the header (40–90%). It applies immediately and is stored in `settings.json`; `animals.cat_conf` is only the default. In practice anything below 60% is usually a false positive, so 60–70% is a sensible range.

Cat watching is per camera. In the camera settings panel, every camera except the active plate camera shows a **貓偵測** button; press it to start or stop a cat sentinel for that camera. A sentinel captures its own low-resolution stream (about 1 fps) and only runs the model when it sees motion, so several cameras can be watched at the same time. The active camera keeps watching cats inside the main pipeline. Sentinels start automatically after a restart if their camera still has cat watch enabled, and each sighting is stored as a `cat` event tagged with the camera id.

If a camera serves H.265 (HEVC), the copied clip would not play in most browsers, so the service checks the codec after each recording and transcodes HEVC clips to H.264 with VideoToolbox (hardware, near-zero CPU). H.264 clips are left untouched.

Click **歷史截圖保留 … 天** under the page title to change how long snapshots and history rows are kept (1–365 days). Applying a value prunes older entries immediately, and the hourly cleanup keeps using it. The value is stored in `retention.json` next to the database, so it survives restarts; `retain_days` in `config.yaml` is only the default for a fresh install.

## 8. Service management

macOS:

```sh
launchctl kickstart -k gui/$(id -u)/com.local.alpr-gate
tail -f ~/Library/Logs/alpr-gate/stdout.log
```

Linux:

```sh
sudo systemctl restart alpr-gate
sudo journalctl -u alpr-gate -f
```

## 9. Troubleshooting

- **503 on `/health`**: no frame yet, wrong RTSP credentials, or ffmpeg cannot reach go2rtc.
- **UI live view works but ALPR has no frames**: the ALPR stream and live stream are different; check `cameras.*.rtsp_url`.
- **No plate events**: check `plates.allow`, the gate line, `enter_direction`, and the ROI. Keep the line inside the ROI.
- **Repeated events for a parked car**: raise `gate.votes`, `dedupe_seconds`, or move the line away from the parking position.
- **High CPU**: close the live UI when not needed, lower the ALPR stream to 1280×720, or crop in ffmpeg before the raw output.
