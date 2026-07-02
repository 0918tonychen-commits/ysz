from flask import Flask, render_template, request, jsonify
from datetime import datetime, timedelta
import time
import sqlite3
import json
import os
import threading
import requests

app = Flask(__name__)

DB_FILE = "server_data.db"

# ── Discord 警報通知設定 ──
# Webhook 網址視為機密，優先從環境變數讀取（Render 後台設定）。
# 沒設定時 alerts 只會印在 log、不會送出，不影響其他功能運作。
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# ── 警報規則 ──
# 每個感測器代號對應「上限值、顯示名稱、單位」。數值超過 max 就發警報。
ALERT_RULES = {
    "c":    {"max": 1000, "label": "CO₂",   "unit": "ppm"},
    "pm25": {"max": 35,   "label": "PM2.5", "unit": "µg/m³"},
    "pm10": {"max": 150,  "label": "PM10",  "unit": "µg/m³"},
    "t":    {"max": 40,   "label": "溫度",  "unit": "°C"},
}
OFFLINE_TIMEOUT = 180     # 節點超過幾秒沒回報就判定離線（3 分鐘）
ALERT_COOLDOWN = 600      # 同一個警報幾秒內不重複發送（10 分鐘，避免洗版）

def init_db():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS latest (
            node_id TEXT PRIMARY KEY,
            data TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS history (
            node_id TEXT PRIMARY KEY,
            data TEXT
        )
    ''')
    conn.commit()
    conn.close()

def load_state():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    cursor = conn.cursor()
    latest = {node_id: json.loads(data) for node_id, data in cursor.execute("SELECT node_id, data FROM latest")}
    history = {node_id: json.loads(data) for node_id, data in cursor.execute("SELECT node_id, data FROM history")}
    conn.close()
    return latest, history

def save_node_state(node_id):
    """ 將單一節點的最新狀態與歷史紀錄寫回 SQLite，供重啟後還原 """
    conn = sqlite3.connect(DB_FILE, timeout=10)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO latest (node_id, data) VALUES (?, ?) ON CONFLICT(node_id) DO UPDATE SET data = excluded.data",
        (node_id, json.dumps(latest_data[node_id]))
    )
    cursor.execute(
        "INSERT INTO history (node_id, data) VALUES (?, ?) ON CONFLICT(node_id) DO UPDATE SET data = excluded.data",
        (node_id, json.dumps(history_data[node_id]))
    )
    conn.commit()
    conn.close()

# 全域狀態儲存（開機時從 SQLite 還原，重啟/休眠喚醒後資料不會歸零）
init_db()
latest_data, history_data = load_state()
last_seen = {}

# ── 警報去重狀態（存記憶體，伺服器重啟後歸零可接受）──
alert_cooldowns = {}   # "node:sensor" -> 上次發送時間戳，用來做冷卻，避免同一警報洗版
offline_nodes = set()  # 目前已判定離線、且已發過警報的節點，避免重複發離線警報

def send_discord_alert(title, message, color=0xFF0000):
    """ 把警報以 Discord embed 格式送出。放在背景執行緒送，避免拖慢 /update 回應。 """
    if not DISCORD_WEBHOOK_URL:
        print(f"🔔 [警報-未設定Discord] {title}｜{message}")
        return

    def _send():
        payload = {
            "embeds": [{
                "title": title,
                "description": message,
                "color": color,
                "footer": {"text": "LORA 環境監測系統"},
                "timestamp": datetime.utcnow().isoformat()
            }]
        }
        try:
            requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5)
        except requests.RequestException as e:
            print(f"⚠️ [警報發送失敗] {e}")

    threading.Thread(target=_send, daemon=True).start()

def check_threshold_alerts(node_id, sensor_batch):
    """ 檢查這批數值有沒有超標，超過門檻且不在冷卻期就發警報 """
    now = time.time()
    for sensor, rule in ALERT_RULES.items():
        if sensor not in sensor_batch:
            continue
        try:
            value = float(sensor_batch[sensor])
        except (ValueError, TypeError):
            continue
        if value > rule["max"]:
            key = f"{node_id}:{sensor}"
            if now - alert_cooldowns.get(key, 0) < ALERT_COOLDOWN:
                continue  # 還在冷卻期，先不重複發
            alert_cooldowns[key] = now
            send_discord_alert(
                title=f"⚠️ 數值超標警報｜{node_id.upper()}",
                message=f"**{rule['label']}** 目前 **{value} {rule['unit']}**，已超過安全上限 {rule['max']} {rule['unit']}。",
                color=0xFF003C
            )

def check_offline_alerts():
    """ 掃描所有節點，找出太久沒回報的判定離線並發警報；恢復回報的發復線通知 """
    now = time.time()
    for node_id, ts in list(last_seen.items()):
        silent = now - ts
        if silent > OFFLINE_TIMEOUT and node_id not in offline_nodes:
            offline_nodes.add(node_id)
            send_discord_alert(
                title=f"🔌 節點離線警報｜{node_id.upper()}",
                message=f"節點已超過 **{int(silent)} 秒** 沒有回報數據，可能斷線或故障。",
                color=0xFFA500
            )

@app.route('/')
def index():
    return render_template('index.html')

@app.get("/api/all_data")
def get_all_data():
    now = time.time()
    # 判斷 60 秒內是否有數據更新
    status = "online" if any(now - ts < 60 for ts in last_seen.values()) else "offline"
    return jsonify({
        "status": status,
        "history": history_data,
        "latest": latest_data,
    })

@app.route('/update', methods=['POST'])
def update():
    global latest_data, history_data
    req = request.json
    if not req or "node" not in req: return {"status": "error"}, 400

    node_id = req["node"]
    sensor_batch = req["data"]
    last_seen[node_id] = time.time()

    # 若此節點先前被判定離線，現在又回報了 → 發「恢復連線」通知並解除離線標記
    if node_id in offline_nodes:
        offline_nodes.discard(node_id)
        send_discord_alert(
            title=f"✅ 節點恢復連線｜{node_id.upper()}",
            message="節點已重新開始回報數據，連線恢復正常。",
            color=0x39FF14
        )

    # 優先採用封包自帶的原始時間戳（斷線補傳資料也一樣），確保時間序列不因補傳而錯亂
    try:
        event_dt = datetime.utcfromtimestamp(float(req["recorded_at"]))
    except (KeyError, TypeError, ValueError):
        event_dt = datetime.utcnow()
    current_time = (event_dt + timedelta(hours=8)).strftime("%H:%M:%S")

    if node_id not in history_data:
        history_data[node_id] = {"labels": []}

    node_hist = history_data[node_id]
    node_hist["labels"].append(current_time)

    # 硬體簡稱對應前端 Key
    mapping = {"t": "temp", "h": "hum", "c": "co2", "v": "volt"}

    node_latest = latest_data.setdefault(node_id, {})

    # 更新數值並寫入歷史
    for key, val in sensor_batch.items():
        num_val = round(float(val), 2)
        node_latest[key] = str(num_val)

        hist_key = mapping.get(key, key)
        if hist_key not in node_hist:
            # 新感測器：先用 0 補齊先前的長度
            node_hist[hist_key] = [0.0] * (len(node_hist["labels"]) - 1)
        node_hist[hist_key].append(num_val)

    # 數據長度校準：確保所有陣列與 labels 等長
    for k in node_hist.keys():
        if len(node_hist[k]) < len(node_hist["labels"]):
            node_hist[k].append(node_hist[k][-1] if node_hist[k] else 0.0)
        if len(node_hist[k]) > 100: node_hist[k].pop(0)

    save_node_state(node_id)

    # 資料存好後才判斷警報：先看這批數值有沒有超標，再掃描有沒有節點離線
    check_threshold_alerts(node_id, sensor_batch)
    check_offline_alerts()

    return {"status": "success"}, 200

@app.get("/api/test-alert")
def test_alert():
    """ 手動測試：打開這個網址就會送一則測試警報到 Discord，用來確認設定成功 """
    if not DISCORD_WEBHOOK_URL:
        return {"status": "error", "message": "尚未設定 DISCORD_WEBHOOK_URL 環境變數"}, 400
    send_discord_alert(
        title="🔔 測試通知",
        message="如果你在 Discord 看到這則訊息，代表警報通知已設定成功！",
        color=0x0088FF
    )
    return {"status": "success", "message": "測試通知已送出，請查看 Discord 頻道"}, 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)