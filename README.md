# YZC LoRa 環境監測

本專案包含 Windows Python Gateway、SQLite store-and-forward、Flask/PostgreSQL
後端與瀏覽器監控頁面。正式「上行為主、只保留單跳 ACK 備援」的 S05 韌體位於
`firmware/S05_from_S03/`。

## 系統資料流

```text
Arduino 感測節點 → LoRa 中繼 → Windows Gateway → SQLite outbox
                                         ↓
瀏覽器監控頁面 ← Flask API ← PostgreSQL ← HTTP 上傳
```

## 資料協定

```json
{
  "event_id": "7ba7a7bd-f240-4b7e-a268-ea49037e515c",
  "node": "s10",
  "recorded_at": 1784800000.0,
  "data": {
    "temperature": 25.3,
    "humidity": 60.0,
    "co2": 450.0
  },
  "meta": {
    "mcount": 42,
    "via": ["s02"],
    "rssi": -80,
    "snr": 6.5,
    "hop_rssi": -73,
    "hop_snr": 5.2,
    "loss": 1.2
  }
}
```

`data` 只能包含環境感測數值；路由與無線資訊放在 `meta`。舊韌體的
`t/h/c/v` 和 `gw_rssi/gw_snr/msg` 會由 Gateway 正規化。

## 安裝

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

請自行填寫 `.env`，不要將 `.env` 或任何真實密鑰提交到 Git。

後端與 Gateway 的 `LORA_API_KEY` 必須完全相同。可在 PowerShell 產生：

```powershell
$env:LORA_API_KEY = python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## 本機後端與資料庫

已安裝 Docker Desktop 的電腦可以用一個指令啟動 PostgreSQL 和網站：

```powershell
$env:LORA_API_KEY = "僅供本機開發的密鑰"
docker compose up --build
```

等待 `website` healthy 後，在另一個終端執行端到端 smoke test：

```powershell
$env:LORA_API_KEY = "僅供本機開發的密鑰"
python scripts/e2e_smoke.py
```

測試會實際驗證資料庫健康狀態、API 驗證、遙測寫入、`event_id` 去重與資料讀回。
停止服務使用 `docker compose down`；只有確定不再需要本機資料時才使用
`docker compose down --volumes`。

## 執行

Gateway：

```powershell
$env:LORA_COM_PORT = "COM3"
python bridge.py
```

Flask 開發環境：

```powershell
python main.py
```

正式環境：

```text
gunicorn main:app
```

健康檢查：

```text
GET /healthz
```

成功回應為 HTTP 200；資料庫無法連線時為 HTTP 503。

## SQLite outbox

每個封包一解析完就先寫進 SQLite，之後才由上傳執行緒送出。SQLite 是**第一站**
而不是失敗後的退路，所以停電或當掉時不會有「還沒送出、也沒有任何紀錄」的資料。

Gateway 啟動時會顯示：

```text
Gateway cache ready: 3 pending, 1 quarantined
```

- `pending`：已落盤、等待送出。
- `quarantined`：壞 JSON 或被後端永久拒絕的資料，保留供診斷但不阻塞補傳。

暫時性網路問題、HTTP 408、425、429 與 5xx 都不會刪除資料。送出成功才刪除，
所以每筆最少送達一次；後端以 `event_id` 去重。

寫不進 outbox 時 gateway 會以非零狀態結束，交給服務管理器重啟 —— 繼續收封包
卻無處可放，只會安靜地掉資料。

## 測試

```powershell
pytest -q
```

測試不需要真實序列埠、Neon 或 Render。

## Arduino 韌體狀態

目前的 MKR WAN 1310（`arduino:samd:mkrwan1310`）韌體配置為：

- `firmware/S05_from_S03/S05_from_S03.ino`：正式 S05，只有中繼 ACK，沒有控制下行。
- 桌面專題 `node_firmware_2_/templates/`：S02～S05 的完整燒錄範本。

`.arduino_build/` 是產生物，不是原始碼，之後不應加入 Git。舊版下行韌體的
還原方式記錄在 `firmware/archive/S05_DOWNLINK_VERSION.md`。

實機驗收至少要確認：感測器讀值、LoRa 上行、單跳 ACK、嚴格主備切換、Gateway
斷線補傳及後端去重。軟體測試通過不能取代射頻、供電、天線與序列埠測試。
