from flask import Flask, render_template, request, jsonify
from datetime import datetime, timedelta
import time

app = Flask(__name__)

# 全域狀態儲存
all_sensors = {}   
history_data = {}  
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
        **all_sensors
    })

@app.route('/update', methods=['POST'])
def update():
    global all_sensors, history_data
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

    # 更新數值並寫入歷史
    for key, val in sensor_batch.items():
        num_val = round(float(val), 2)
        all_sensors[f"{node_id}_{key}"] = {"val": str(num_val)}
        
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

    return {"status": "success"}, 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)