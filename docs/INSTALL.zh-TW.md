# 安裝說明（繁體中文）

本文件只說明安裝與設定步驟。程式、註解與技術文件以英文為準。請把 `ALPR_HOST`、`GO2RTC_HOST`、`HOME_ASSISTANT_HOST`、串流名稱與 entity ID 換成你自己的值。

## 1. 準備 go2rtc 串流

建立一個給 ALPR 的 2fps 串流，以及一個給瀏覽器即時看的 15fps 串流。只有 ALPR 串流會持續被消耗。

```yaml
streams:
  driveway:
    - rtsp://USER:PASSWORD@CAMERA_IP:554/stream1

  driveway_alpr:
    - ffmpeg:driveway#video=h264#hardware=videotoolbox#raw=-r 2 -g 4 -bf 0 -vf hwdownload,format=nv12,scale=1920:1080

  driveway_h264:
    - ffmpeg:driveway#video=h264#hardware=videotoolbox#raw=-r 15 -g 30 -bf 0 -vf hwdownload,format=nv12,scale=1280:720
```

Linux 請移除 `hardware=videotoolbox` 與硬體專用的 `hwdownload,format=nv12`，或改成你的硬體編碼參數。

服務要用的 RTSP URL 會是：

```text
rtsp://GO2RTC_USER:GO2RTC_PASSWORD@GO2RTC_HOST:8554/driveway_alpr
```

即時畫面 URL 會是 `http://GO2RTC_HOST:1984/stream.html?src=driveway_h264`。

## 2. 安裝服務

### macOS

```sh
git clone https://github.com/kingwap99/ha-alpr-gate.git
cd ha-alpr-gate
./scripts/install_macos.sh
```

安裝腳本會建立 `~/alpr-gate`、安裝 Python 環境、在沒有 `config.yaml` 時複製範例，並載入 launchd 使用者服務。第一次啟動前請先改好設定。

手動安裝：

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

把 `deploy/alpr-gate.service` 複製到 `/etc/systemd/system/`，修改使用者與路徑，然後：

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now alpr-gate
```

## 3. 修改設定

編輯 `config.yaml`，至少修改：

- `camera`／`cameras.*.rtsp_url`：ALPR 串流網址，包含 go2rtc 帳密。
- `cameras.*.live_url`：瀏覽器要開的即時畫面網址。
- `active_camera`：第一次啟動要用的鏡頭。
- `cameras.*.line_y`、`enter_direction`：全畫面座標的判斷線與進入方向。
- `plates.allow`：要放行的車牌；空清單代表只觀察、全部記錄。
- `ha.webhook_url`：Home Assistant webhook；空字串代表 dry-run。
- `ha.public_base_url`：HA 與手機可以載入截圖的服務網址。

`config.yaml` 內有攝影機帳密與 webhook，請保持權限 `600`。

ROI 是 0–1 的標準化矩形：

```yaml
roi: {x: 0.10, y: 0.25, width: 0.80, height: 0.65}
```

也可以直接在 Web UI 按 **偵測範圍** 拖曳。ROI 會套在 motion 與車輛偵測之前，能減少模型執行次數，但不會降低 ffmpeg 解完整張畫面的成本。

## 4. 第一次啟動

```sh
cd ~/alpr-gate
venv/bin/python alpr_gate.py --config config.yaml --dry-run
```

打開 `http://ALPR_HOST:8099/`，確認：

- 狀態顯示服務正常。
- 即時畫面能播放。
- 已知車牌跨越判斷線時會產生事件。
- 點歷史卡片可以放大截圖。

第一個畫面可能要等 10–30 秒，因為 ffmpeg 在等 keyframe；這段時間 `/health` 會回 503。

## 5. 接上 Home Assistant

建立一個隨機 webhook ID，把 `examples/home_assistant/automation.yaml` 的內容貼到 HA automation，替換：

- `REPLACE_WITH_RANDOM_ID`
- `AB1234` 改成你的車牌
- `switch.your_outside_light`
- `switch.garage_door_up`
- `binary_sensor.garage_door_contact`
- `driveway` 改成正式鏡頭的 ID

如果要顯示最後車牌／方向／時間，建立 `examples/home_assistant/helpers.yaml` 內的三個 helper。

服務設定：

```yaml
ha:
  webhook_url: "http://HOME_ASSISTANT_HOST:8123/api/webhook/REPLACE_WITH_RANDOM_ID"
```

automation 使用 `local_only: true`。ALPR 服務與 HA 應在同一個受信任的 LAN。

### 加到 HA 側邊欄

1. 設定 → 儀表板 → 新增儀表板。
2. 選 **網頁面板**。
3. 網址填 `http://ALPR_HOST:8099/`。
4. 設定標題與 URL path，建立。

本機 HTTP 的 HA 可以正常嵌入；若你從外網 HTTPS 開 HA，瀏覽器可能阻擋 HTTP iframe，這時直接開 ALPR 網址。

## 6. 切換鏡頭與 ROI

UI 的「監測鏡頭」選好後要按 **套用** 才會切換；這可避免重新整理頁面時誤送切換 request。切換後 ALPR 來源與即時畫面一起換，選擇存在 `active_camera.json`，重啟後保留。

建議先用有固定停車的測試鏡頭驗證車牌與 webhook，再切到正式鏡頭。切換後約需 10–30 秒重新連線。

開車庫門是實體動作。請先確認判斷線、`enter_direction`、ROI 都正確，並且人在現場、可隨時停止，再做第一次自動開門測試。

## 7. 服務管理

macOS：

```sh
launchctl kickstart -k gui/$(id -u)/com.local.alpr-gate
tail -f ~/Library/Logs/alpr-gate/stdout.log
```

Linux：

```sh
sudo systemctl restart alpr-gate
sudo journalctl -u alpr-gate -f
```

## 8. 常見問題

- **`/health` 回 503**：還沒收到第一個畫面、RTSP 帳密錯誤，或 ffmpeg 連不到 go2rtc。
- **即時畫面正常但 ALPR 沒畫面**：即時與 ALPR 是不同串流，檢查 `cameras.*.rtsp_url`。
- **沒有車牌事件**：檢查 `plates.allow`、判斷線、`enter_direction`、ROI；判斷線必須在 ROI 內。
- **停著的車一直重複事件**：提高 `gate.votes`、`dedupe_seconds`，或把判斷線移開停車位置。
- **CPU 太高**：關掉即時 UI、把 ALPR 串流降到 1280×720，或在 ffmpeg 先 crop 再輸出。
