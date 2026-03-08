from flask import Flask, render_template, request, jsonify
from datetime import datetime, timedelta  # 核心修正：用於處理台灣時區
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
        
        # 數值處理與清潔
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

        # --- 核心修改：強制台灣時區 (UTC+8) ---
        # datetime.utcnow() 抓取伺服器 UTC 時間，再加上 8 小時
        tw_time = datetime.utcnow() + timedelta(hours=8)
        current_time = tw_time.strftime("%H:%M:%S")

        # --- 確保數據同步存入 ---
        if data_type == "t": 
            node_hist["temp"].append(num_val)
            node_hist["labels"].append(current_time) # 隨溫度更新時間標籤
        elif data_type == "h": 
            node_hist["hum"].append(num_val)

        # 統一限制長度為 100 筆，防止記憶體溢出，且維持 temp/hum/labels 長度一致
        max_len = 100
        for key in ["temp", "hum", "labels"]:
            while len(node_hist[key]) > max_len:
                node_hist[key].pop(0)

        return {"status": "success"}, 200
    
    return {"status": "error"}, 400

if __name__ == '__main__':
    # 這裡 port 10000 是為了配合 Render 部署
    app.run(host='0.0.0.0', port=10000)
