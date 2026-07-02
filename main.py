from flask import Flask, render_template, request, jsonify
from datetime import datetime, timedelta
import time
import sqlite3
import json

app = Flask(__name__)

DB_FILE = "server_data.db"

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

    # 台灣時間格式化
    current_time = (datetime.utcnow() + timedelta(hours=8)).strftime("%H:%M:%S")

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

    return {"status": "success"}, 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)