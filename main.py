from flask import Flask, render_template, request, jsonify
from datetime import datetime, timedelta
import time

app = Flask(__name__)

# --- 1. 配置區：感測器單位映射表 ---
# 這裡建議把硬體傳來的簡稱 (如 c) 對應到單位的 Key (如 co2)
UNIT_MAP = {
    "t": "°C",      # 溫度
    "h": "%",       # 濕度
    "c": "ppm",     # 二氧化碳 (硬體傳的是 c)
    "co2": "ppm",
    "pm25": "μg/m³",
    "pm10": "μg/m³"
}

# --- SCADA 數據中心 ---
all_sensors = {}   
history_data = {}  
last_seen_map = {} 

@app.route('/')
def index():
    return render_template('index.html')

@app.get("/api/all_data")
def get_all_data():
    now = time.time()
    system_status = "offline"
    
    for ts in last_seen_map.values():
        if now - ts < 40: 
            system_status = "online"
            break
            
    response_data = {
        "status": system_status,
        "history": history_data,
        "units": UNIT_MAP  
    }
    response_data.update(all_sensors)
    return jsonify(response_data)

@app.route('/update', methods=['POST'])
def update():
    global all_sensors, history_data, last_seen_map
    json_data = request.json 
    
    # --- 新邏輯：解析批次格式 ---
    # 預期格式: {"node": "s02", "data": {"t": "26.2", "h": "71.0", "c": "686"}, "timestamp": 12345}
    if not json_data or "node" not in json_data or "data" not in json_data:
        return {"status": "error", "message": "Invalid batch format"}, 400

    node_id = json_data["node"].lower()
    sensor_batch = json_data["data"] # 這是一個字典 {"t": "26.2", "h": "71.0", ...}
    
    # 更新最後在線時間
    last_seen_map[node_id] = time.time()

    # 準備台灣時間標籤 (一整包數據用同一個時間點)
    tw_time = datetime.utcnow() + timedelta(hours=8)
    current_time = tw_time.strftime("%H:%M:%S")

    if node_id not in history_data:
        history_data[node_id] = {"labels": []}

    node_hist = history_data[node_id]
    
    # 標籤只加一次
    node_hist["labels"].append(current_time)

    # --- 遍歷這包數據中的所有感測器 ---
    for sensor_key, val in sensor_batch.items():
        # 1. 清理數據並轉為浮點數
        try:
            num_val = round(float(val), 2)
        except (ValueError, TypeError):
            num_val = 0.0

        # 2. 更新即時數據庫 (維持 s02_t 格式以相容舊前端)
        full_id = f"{node_id}_{sensor_key}"
        all_sensors[full_id] = {"val": str(num_val)}

        # 3. 更新歷史紀錄
        # 映射前端習慣的名稱 (t -> temp, h -> hum)
        mapping = {"t": "temp", "h": "hum", "c": "co2"}
        hist_key = mapping.get(sensor_key, sensor_key)

        if hist_key not in node_hist:
            # 如果是新指標，前面缺失的數據補 0 或 None 保持長度一致
            node_hist[hist_key] = [0.0] * (len(node_hist["labels"]) - 1)
        
        node_hist[hist_key].append(num_val)

    # --- 補齊長度與限制長度 ---
    max_len = 100
    for key in node_hist.keys():
        # 補齊那些在這次批次中沒出現的感測器數據，確保與 labels 長度對齊
        if len(node_hist[key]) < len(node_hist["labels"]):
            node_hist[key].append(node_hist[key][-1] if node_hist[key] else 0.0)
            
        # 限制長度
        while len(node_hist[key]) > max_len:
            node_hist[key].pop(0)

    return {"status": "success"}, 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)