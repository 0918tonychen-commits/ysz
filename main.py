from flask import Flask, render_template, request, jsonify
from datetime import datetime, timedelta
import time
import json
import os
import threading
import requests
import psycopg
from dotenv import load_dotenv

app = Flask(__name__)

# ── 資料庫連線設定（Neon PostgreSQL）──
# 本機開發：從專案根目錄 .env 讀取；Render 部署：從後台 Environment 讀取。
# 換成外部資料庫後，重新部署／休眠／重啟都不會再遺失資料。
load_dotenv()
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    raise RuntimeError(
        "未設定 DATABASE_URL！本機請在專案根目錄的 .env 填入 Neon 連線字串，"
        "Render 請在服務的 Environment 頁面設定。"
    )

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

# ── 完整歷史紀錄保留設定 ──
RETENTION_SECONDS = 7 * 24 * 60 * 60         # 保留最近 7 天的完整原始資料
PRUNE_INTERVAL = 300                         # 每 5 分鐘才真的執行一次清除，避免每個請求都跑 DELETE
HOURLY_RETENTION_SECONDS = 365 * 24 * 60 * 60  # 降採樣後的每小時統計保留 365 天

# ── 共用資料庫執行器 ──
# 維持一條長連線重複使用（每次重開連線到 Neon 要多花數百毫秒）；
# Neon 免費方案閒置一段時間會休眠、喚醒時舊連線會失效，所以失敗時自動重連再試一次。
_db_lock = threading.Lock()
_db_conn = None

def db_execute(query, params=None, fetch=False, many=False):
    global _db_conn
    with _db_lock:
        for attempt in (1, 2):
            try:
                if _db_conn is None or _db_conn.closed:
                    _db_conn = psycopg.connect(DATABASE_URL, autocommit=True)
                with _db_conn.cursor() as cursor:
                    if many:
                        cursor.executemany(query, params or [])
                        return None
                    cursor.execute(query, params or ())
                    return cursor.fetchall() if fetch else None
            except psycopg.OperationalError:
                # 連線失效（Neon 休眠喚醒或網路中斷）：丟掉舊連線，重連再試一次
                try:
                    if _db_conn:
                        _db_conn.close()
                except psycopg.Error:
                    pass
                _db_conn = None
                if attempt == 2:
                    raise

def init_db():
    db_execute('''
        CREATE TABLE IF NOT EXISTS latest (
            node_id TEXT PRIMARY KEY,
            data TEXT
        )
    ''')
    db_execute('''
        CREATE TABLE IF NOT EXISTS history (
            node_id TEXT PRIMARY KEY,
            data TEXT
        )
    ''')
    # 完整歷史紀錄：每一筆數值都存成獨立一列，不受即時圖表 100 筆上限影響，
    # 只靠 recorded_at 保留最近 RETENTION_SECONDS（目前 7 天），舊資料自動清除
    db_execute('''
        CREATE TABLE IF NOT EXISTS readings (
            id BIGSERIAL PRIMARY KEY,
            node_id TEXT NOT NULL,
            sensor TEXT NOT NULL,
            value DOUBLE PRECISION NOT NULL,
            recorded_at DOUBLE PRECISION NOT NULL
        )
    ''')
    db_execute('CREATE INDEX IF NOT EXISTS idx_readings_node_time ON readings (node_id, recorded_at)')
    # 降採樣長期保存：原始資料滿 7 天被清除前，先壓縮成「每小時一筆統計」存進這張表。
    # 存 sum 而非 avg，才能在同一小時分多批壓縮時正確合併（讀取時再算平均）。
    db_execute('''
        CREATE TABLE IF NOT EXISTS readings_hourly (
            node_id TEXT NOT NULL,
            sensor TEXT NOT NULL,
            bucket_ts DOUBLE PRECISION NOT NULL,
            sum_value DOUBLE PRECISION NOT NULL,
            min_value DOUBLE PRECISION NOT NULL,
            max_value DOUBLE PRECISION NOT NULL,
            sample_count BIGINT NOT NULL,
            PRIMARY KEY (node_id, sensor, bucket_ts)
        )
    ''')

def load_state():
    latest = {node_id: json.loads(data) for node_id, data in db_execute("SELECT node_id, data FROM latest", fetch=True)}
    history = {node_id: json.loads(data) for node_id, data in db_execute("SELECT node_id, data FROM history", fetch=True)}
    return latest, history

def save_node_state(node_id):
    """ 將單一節點的最新狀態與歷史紀錄寫回資料庫，供重啟後還原 """
    db_execute(
        "INSERT INTO latest (node_id, data) VALUES (%s, %s) ON CONFLICT (node_id) DO UPDATE SET data = EXCLUDED.data",
        (node_id, json.dumps(latest_data[node_id]))
    )
    db_execute(
        "INSERT INTO history (node_id, data) VALUES (%s, %s) ON CONFLICT (node_id) DO UPDATE SET data = EXCLUDED.data",
        (node_id, json.dumps(history_data[node_id]))
    )

_last_prune_time = 0

def log_reading(node_id, sensor_values, recorded_at):
    """ 把這批數值逐筆寫進完整歷史紀錄表，並每隔 PRUNE_INTERVAL 秒清掉超過保留期限的舊資料 """
    global _last_prune_time
    rows = [(node_id, sensor, value, recorded_at) for sensor, value in sensor_values.items()]
    db_execute(
        "INSERT INTO readings (node_id, sensor, value, recorded_at) VALUES (%s, %s, %s, %s)",
        rows, many=True
    )

    now = time.time()
    if now - _last_prune_time > PRUNE_INTERVAL:
        cutoff = now - RETENTION_SECONDS
        # 降採樣：把即將過期的原始資料壓縮成每小時統計後才刪除。
        # 整段是單一 SQL 語句（CTE），資料庫保證原子性：
        # 不會出現「壓縮成功但刪除失敗 → 下輪重複壓縮 → 統計翻倍」的錯誤。
        db_execute('''
            WITH expired AS (
                DELETE FROM readings WHERE recorded_at < %s
                RETURNING node_id, sensor, value, recorded_at
            )
            INSERT INTO readings_hourly (node_id, sensor, bucket_ts, sum_value, min_value, max_value, sample_count)
            SELECT node_id, sensor, FLOOR(recorded_at / 3600) * 3600,
                   SUM(value), MIN(value), MAX(value), COUNT(*)
            FROM expired
            GROUP BY node_id, sensor, FLOOR(recorded_at / 3600) * 3600
            ON CONFLICT (node_id, sensor, bucket_ts) DO UPDATE SET
                sum_value = readings_hourly.sum_value + EXCLUDED.sum_value,
                min_value = LEAST(readings_hourly.min_value, EXCLUDED.min_value),
                max_value = GREATEST(readings_hourly.max_value, EXCLUDED.max_value),
                sample_count = readings_hourly.sample_count + EXCLUDED.sample_count
        ''', (cutoff,))
        # 小時統計也有保留上限（365 天），超過的才真正丟棄
        db_execute("DELETE FROM readings_hourly WHERE bucket_ts < %s", (now - HOURLY_RETENTION_SECONDS,))
        _last_prune_time = now

# 全域狀態儲存（開機時從資料庫還原，重新部署/休眠/重啟都不會遺失）
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

@app.get("/api/history_range/<node_id>")
def get_history_range(node_id):
    """ 查詢某節點過去最多 7 天的完整原始資料（不受即時圖表 100 筆上限影響） """
    days = request.args.get("days", default=7, type=int)
    days = max(1, min(days, 7))  # 目前只保留 7 天，超過範圍就夾住
    cutoff = time.time() - days * 86400

    rows = db_execute(
        "SELECT sensor, value, recorded_at FROM readings WHERE node_id = %s AND recorded_at >= %s ORDER BY recorded_at ASC",
        (node_id, cutoff), fetch=True
    )

    timestamps = sorted(set(r[2] for r in rows))
    labels = [(datetime.utcfromtimestamp(ts) + timedelta(hours=8)).strftime("%m/%d %H:%M:%S") for ts in timestamps]

    by_sensor = {}
    for sensor, value, ts in rows:
        by_sensor.setdefault(sensor, {})[ts] = value

    result = {"labels": labels}
    for sensor, ts_map in by_sensor.items():
        result[sensor] = [ts_map.get(ts) for ts in timestamps]

    return jsonify(result)

@app.get("/api/history_hourly/<node_id>")
def get_history_hourly(node_id):
    """ 查詢降採樣後的長期資料（每小時統計，最多回溯 365 天）。
        每個感測器回傳三組陣列：平均值（感測器名）、最低（_min）、最高（_max）。 """
    days = request.args.get("days", default=30, type=int)
    days = max(1, min(days, 365))
    cutoff = time.time() - days * 86400

    rows = db_execute(
        """SELECT sensor, bucket_ts, sum_value / sample_count, min_value, max_value
           FROM readings_hourly WHERE node_id = %s AND bucket_ts >= %s ORDER BY bucket_ts ASC""",
        (node_id, cutoff), fetch=True
    )

    timestamps = sorted(set(r[1] for r in rows))
    labels = [(datetime.utcfromtimestamp(ts) + timedelta(hours=8)).strftime("%m/%d %H:00") for ts in timestamps]

    by_sensor = {}
    for sensor, ts, avg_v, min_v, max_v in rows:
        by_sensor.setdefault(sensor, {})[ts] = (round(avg_v, 2), min_v, max_v)

    result = {"labels": labels}
    for sensor, ts_map in by_sensor.items():
        result[sensor] = [ts_map[ts][0] if ts in ts_map else None for ts in timestamps]
        result[f"{sensor}_min"] = [ts_map[ts][1] if ts in ts_map else None for ts in timestamps]
        result[f"{sensor}_max"] = [ts_map[ts][2] if ts in ts_map else None for ts in timestamps]

    return jsonify(result)

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
        event_ts = float(req["recorded_at"])
    except (KeyError, TypeError, ValueError):
        event_ts = time.time()
    event_dt = datetime.utcfromtimestamp(event_ts)
    current_time = (event_dt + timedelta(hours=8)).strftime("%H:%M:%S")

    if node_id not in history_data:
        history_data[node_id] = {"labels": []}

    node_hist = history_data[node_id]
    node_hist["labels"].append(current_time)

    # 硬體簡稱對應前端 Key
    mapping = {"t": "temp", "h": "hum", "c": "co2", "v": "volt"}

    node_latest = latest_data.setdefault(node_id, {})
    parsed_values = {}

    # 更新數值並寫入歷史
    for key, val in sensor_batch.items():
        num_val = round(float(val), 2)
        node_latest[key] = str(num_val)
        parsed_values[key] = num_val

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
    log_reading(node_id, parsed_values, event_ts)

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