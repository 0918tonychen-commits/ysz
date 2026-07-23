# YZC LoRa 環境監測

本專案包含 Windows Python Gateway、SQLite store-and-forward、Flask/PostgreSQL
後端與瀏覽器監控頁面。Arduino 韌體目前不在此 repository。

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

## SQLite 快取

Gateway 啟動時會顯示：

```text
Gateway cache ready: 3 pending, 1 quarantined
```

- `pending`：等待補傳。
- `quarantined`：壞 JSON 或被後端永久拒絕的資料，保留供診斷但不阻塞補傳。

暫時性網路問題、HTTP 408、425、429 與 5xx 都不會刪除快取。

## 測試

```powershell
pytest -q
```

測試不需要真實序列埠、Neon 或 Render。
