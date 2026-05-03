from flask import Flask, render_template, request, jsonify
from datetime import datetime, timedelta
import time

app = Flask(__name__)

# 單位映射
UNIT_MAP = {
    "t": "°C", "h": "%", "c": "ppm", "co2": "ppm",
    "pm25": "μg/m³", "pm10": "μg/m³"
}

all_sensors = {}   
history_data = {}  
last_seen_map = {} 

@app.route('/')
def index():
    return render_template('index.html')

@app.get("/api/all_data")
def get_all_data():
    now = time.time()
    system_status = "online" if any(now - ts < 60 for ts in last_seen_map.values()) else "offline"
    return jsonify({
        "status": system_status,
        "history": history_data,
        "units": UNIT_MAP,
        **all_sensors
    })

@app.route('/update', methods=['POST'])
def update():
    global all_sensors, history_data, last_seen_map
    data = request.json 
    if not data or "node" not in data: return {"status": "error"}, 400

    node_id = data["node"]
    sensor_batch = data["data"]
    last_seen_map[node_id] = time.time()
    
    tw_time = datetime.utcnow() + timedelta(hours=8)
    current_time = tw_time.strftime("%H:%M:%S")

    if node_id not in history_data:
        history_data[node_id] = {"labels": []}
    
    node_hist = history_data[node_id]
    node_hist["labels"].append(current_time)

    # 映射表：將硬體簡稱轉為前端 key
    mapping = {"t": "temp", "h": "hum", "c": "co2"}

    for key, val in sensor_batch.items():
        num_val = round(float(val), 2)
        all_sensors[f"{node_id}_{key}"] = {"val": str(num_val)}
        
        hist_key = mapping.get(key, key)
        if hist_key not in node_hist:
            node_hist[hist_key] = [0.0] * (len(node_hist["labels"]) - 1)
        node_hist[hist_key].append(num_val)

    # 長度限制與補齊
    for k in node_hist.keys():
        if len(node_hist[k]) < len(node_hist["labels"]):
            node_hist[k].append(node_hist[k][-1] if node_hist[k] else 0.0)
        if len(node_hist[k]) > 100: node_hist[k].pop(0)

    return {"status": "success"}, 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)