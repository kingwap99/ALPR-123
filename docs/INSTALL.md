# Installation

This guide installs ALPR Gate next to an existing `go2rtc` instance and connects it to Home Assistant. Replace `ALPR_HOST`, `GO2RTC_HOST`, `HOME_ASSISTANT_HOST`, stream names, and entity IDs with your own values.

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
git clone https://github.com/kingwap99/ha-alpr-gate.git
cd ha-alpr-gate
./scripts/install_macos.sh
```

The installer creates `~/alpr-gate`, installs the Python environment, copies the example configuration only if `config.yaml` does not exist, and loads a launchd user agent. Edit the configuration before starting the service for the first time.

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
git clone https://github.com/kingwap99/ha-alpr-gate.git /opt/alpr-gate
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
- A known plate produces an event when it crosses the configured line.
- The history card opens a full snapshot.

The first frame can take 10–30 seconds because ffmpeg waits for a keyframe. The health endpoint returns 503 until the first frame arrives.

## 5. Home Assistant webhook

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

## 6. Camera selection and ROI

The camera selector in the UI changes the ALPR source and the live view. The selection is persisted in `active_camera.json` and survives a restart.

Use a test camera with a known parked car to validate plate recognition and the webhook first. Switch to the production camera only when the line, direction, and ROI are correct. Opening a garage door is a physical action; test it with the door in view and a manual stop available.

## 7. Service management

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

## 8. Troubleshooting

- **503 on `/health`**: no frame yet, wrong RTSP credentials, or ffmpeg cannot reach go2rtc.
- **UI live view works but ALPR has no frames**: the ALPR stream and live stream are different; check `cameras.*.rtsp_url`.
- **No plate events**: check `plates.allow`, the gate line, `enter_direction`, and the ROI. Keep the line inside the ROI.
- **Repeated events for a parked car**: raise `gate.votes`, `dedupe_seconds`, or move the line away from the parking position.
- **High CPU**: close the live UI when not needed, lower the ALPR stream to 1280×720, or crop in ffmpeg before the raw output.
