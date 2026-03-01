from flask import Flask, render_template, request, jsonify
import time

app = Flask(__name__)

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
    
    # 判定門檻：40 秒內有收到任何數據就算 Online
    for ts in last_seen_map.values():
        if now - ts < 40: 
            system_status = "online"
            break
            
    response_data = {
        "status": system_status,
        "history": history_data
    }
    response_data.update(all_sensors)
    return jsonify(response_data)

@app.route('/update', methods=['POST'])
def update():
    global all_sensors, history_data, last_seen_map
    data = request.json 
    
    if data and "id" in data:
        raw_id = data["id"].lower() 
        val_str = str(data.get("val", "0"))
        
        try:
            raw_num = float(val_str.replace("°C", "").replace("%", "").strip())
            num_val = round(raw_num, 1) 
        except (ValueError, TypeError):
            num_val = 0.0

        all_sensors[raw_id] = {"val": str(num_val)}
        
        # 解析 ID (例如 s01_t -> node_id: s01, type: t)
        parts = raw_id.split("_")
        node_id = parts[0]
        data_type = parts[1] if len(parts) > 1 else "t"
        
        last_seen_map[node_id] = time.time() 

        if node_id not in history_data:
            history_data[node_id] = {"temp": [], "hum": [], "labels": []}

        node_hist = history_data[node_id]
        current_time = time.strftime("%H:%M:%S")

        # --- 關鍵修改：確保 temp, hum, labels 長度同步 ---
        if data_type == "t": 
            node_hist["temp"].append(num_val)
            node_hist["labels"].append(current_time) # 隨溫度更新時間標籤
        elif data_type == "h": 
            node_hist["hum"].append(num_val)

        # 統一限制長度為 100 筆，防止記憶體溢出
        max_len = 100
        for key in ["temp", "hum", "labels"]:
            while len(node_hist[key]) > max_len:
                node_hist[key].pop(0)

        return {"status": "success"}, 200
    
    return {"status": "error"}, 400

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
