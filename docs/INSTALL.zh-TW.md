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
git clone https://github.com/kingwap99/ALPR-123.git
cd ALPR-123
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
git clone https://github.com/kingwap99/ALPR-123.git /opt/alpr-gate
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
- 車牌跨越判斷線時產生 `enter` / `exit` 事件；沒有跨越（經過、暫停或直接轉入車位）的車輛，在離開畫面後會補記一筆 `pass` 或 `stop`，所以歷史裡不會漏掉沒開門的車。
- 點歷史卡片可以放大截圖。

第一個畫面可能要等 10–30 秒，因為 ffmpeg 在等 keyframe；這段時間 `/health` 會回 503。

## 5. 使用 Home Assistant 原生整合

把 `custom_components/alpr_gate` 複製到 Home Assistant 的 `config` 目錄，重新啟動 HA。

到 **設定 → 裝置與服務 → 新增整合 → ALPR-123**，輸入 ALPR 主機與連接埠。整合會建立：

- 最後車牌、最後事件、最後時間、今日事件數、鏡頭、信心值 sensor
- 鏡頭／服務 online binary sensor
- `alpr_gate_event` 事件供 automation 觸發

如果 HA 有內部 URL，整合會自動把 webhook 註冊到 ALPR 服務；沒有內部 URL 時改用每 5 秒 polling。

可以使用 `examples/home_assistant/integration_automation.yaml` 的事件觸發 automation，不需要手動建立 webhook ID 或 helper。

若使用 HACS，把這個 repo 加入 custom **Integration** repository，再從 HACS 安裝 **ALPR-123**。

## 6. 可選的 webhook automation

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

## 7. 切換鏡頭、ROI 與截圖保留天數

UI 的「監測鏡頭」選好就直接切換。頁面載入時程式更新選單不會送出切換 request，所以重新整理不會亂跳。切換後 ALPR 來源與即時畫面一起換，選擇存在 `active_camera.json`，重啟後保留。

建議先用有固定停車的測試鏡頭驗證車牌與 webhook，再切到正式鏡頭。切換後約需 10–30 秒重新連線。

設定好的偵測範圍會以綠色虛線框標在即時畫面上，位置依實際顯示的影片區域換算。可用即時畫面卡片右上角的「隱藏偵測範圍／顯示偵測範圍」切換，選擇會記在瀏覽器裡。

標題列的「記錄」選單可以切換記錄模式：**全部經過**（預設，跨越判斷線記 `enter`／`exit`，沒跨越的車輛在離開畫面後補記 `pass`／`stop`）或 **只看跨越**（回到舊行為，只有跨越判斷線才產生事件）。切換立即生效，設定存在 `settings.json`，重啟後保留；`config.yaml` 的 `gate.record_mode` 只是預設值。

在「全部經過」模式下，車子停在畫面中不會被漏掉：靜止超過幾秒且車牌已確認時會記一筆「暫停」。同一車牌連續出現視為同一次停留，只記一筆；要等它超過 10 分鐘沒再出現（`Tracker.ACTIVITY_VISIT_SECONDS`），下一次出現才會記成新的一次。之後若再開動並跨越判斷線，仍會另外記 `enter`／`exit`。靜止偵測每 8 秒才跑一次，所以 `gate.match_seconds`（預設 12 秒）要至少等於這個間隔，否則每次都當成新車、票數永遠湊不滿。

車牌偵測框太小的結果會被丟掉：`models.min_plate_px`（預設 60 像素）與長寬比 1.4–4.5 的檢查，可以擋掉巷道深處或背景被誤認成車牌的情況。如果現場車牌在畫面裡本來就偏小，把這個值調低；若還是有誤判，調高。

如果車輛在兩格之間就開走、沒留下「跨過判斷線」的那一格，追蹤結束時會看最後 10 秒的移動：只要從線的一側移到另一側（位移超過兩倍 `margin`），或在線附近（一倍半 `margin` 內）朝線移動至少一個 `margin` 後消失，仍會補記 `enter`／`exit`，方向以最後一次的移動為準。無尾巷常見的「直進再倒車出去」就是靠這段判定補上離開紀錄。

同一台車在一分鐘內先進入再倒車出去，會留下 `enter` 與 `exit` 兩筆：跨越事件的方向各自去重，通過後也不會清掉已確認的車牌，所以倒車時即使沒再讀到車牌，仍能用先前的確認完成 `exit`。已經有進出兩筆的車輛，不會再多記一筆「經過」。

如果跨越判斷線時車牌一直讀不出來（例如轉進車位、背光或角度太斜），會在等待 `pending_timeout` 後記一筆 `unknown`：保留當下截圖、方向記在 payload 的 `crossing`，歷史清單顯示「未辨識」，HA 也會收到 `event: unknown` 的 webhook。這樣即使 OCR 失敗，也知道有車進出。

若服務重啟時卡在載入車牌模型（日誌停在 `loading plate detector/OCR`、CPU 0%），那是 CoreML 編譯卡住；把 `models.disable_coreml` 設為 `true` 改用 CPU 就能正常啟動（較慢但穩定）。

貓咪會另外以影片記錄。`animals.cat` 開啟後，偵測到貓就從 `cameras.*.clip_url`（建議指向 go2rtc 的 15fps 串流）開始錄影，直接複製 H.264 不重編碼；連續 `cat_leave_seconds` 秒沒再看到貓就停止，最長 `cat_clip_max_seconds`。每一次出現會產生一筆 `cat` 事件、一張標註截圖與一段 MP4，歷史清單可用「貓咪」篩選，點開會直接播放影片。影片與截圖都套用截圖保留天數。

頁面標題下的「歷史截圖保留 N 天」可以直接點開修改（1–365 天）。套用後會立刻清掉超過天數的紀錄與截圖，之後每小時的清理也沿用這個值。設定存在資料夾內的 `retention.json`，重啟後仍然有效；`config.yaml` 的 `retain_days` 只是新安裝時的預設值。

開車庫門是實體動作。請先確認判斷線、`enter_direction`、ROI 都正確，並且人在現場、可隨時停止，再做第一次自動開門測試。

## 8. 服務管理

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

## 9. 常見問題

- **`/health` 回 503**：還沒收到第一個畫面、RTSP 帳密錯誤，或 ffmpeg 連不到 go2rtc。
- **即時畫面正常但 ALPR 沒畫面**：即時與 ALPR 是不同串流，檢查 `cameras.*.rtsp_url`。
- **沒有車牌事件**：檢查 `plates.allow`、判斷線、`enter_direction`、ROI；判斷線必須在 ROI 內。
- **停著的車一直重複事件**：提高 `gate.votes`、`dedupe_seconds`，或把判斷線移開停車位置。
- **CPU 太高**：關掉即時 UI、把 ALPR 串流降到 1280×720，或在 ffmpeg 先 crop 再輸出。
