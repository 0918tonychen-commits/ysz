from flask import Flask, render_template, request, jsonify
from datetime import datetime, timedelta
import time

app = Flask(__name__)

# --- 1. 配置區：感測器單位映射表 ---
UNIT_MAP = {
    "t": "°C",      # 溫度
    "h": "%",       # 濕度
    "co2": "ppm",   # 二氧化碳
    "voc": "mg/m³", # 揮發性有機物
    "vib": "g",     # 震動強度
    "v": "V",       # 電池電壓
    "pm25": "μg/m³",# 新增：PM2.5
    "pm10": "μg/m³" # 新增：PM10
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
    data = request.json 
    
    if data and "id" in data:
        raw_id = data["id"].lower() 
        val_str = str(data.get("val", "0"))
        
        try:
            # 清理常見單位符號並轉為浮點數
            raw_num = float(val_str.replace("°C", "").replace("%", "").replace("ppm", "").strip())
            num_val = round(raw_num, 2)
        except (ValueError, TypeError):
            num_val = 0.0

        all_sensors[raw_id] = {"val": str(num_val)}
        
        # 解析 ID (例如 s03_pm25 -> node_id: s03, data_type: pm25)
        parts = raw_id.split("_")
        node_id = parts[0]
        data_type = parts[1] if len(parts) > 1 else "t"
        
        # 修正：針對前端 index.html 習慣的命名稱呼進行映射
        mapping = {"t": "temp", "h": "hum"}
        hist_key = mapping.get(data_type, data_type) # 如果是 t 轉成 temp, pm25 就維持 pm25
        
        last_seen_map[node_id] = time.time() 

        # --- 動態建立歷史結構 (不再寫死欄位) ---
        if node_id not in history_data:
            history_data[node_id] = {"labels": []}

        node_hist = history_data[node_id]
        
        # 如果這個指標(例如 pm25)還沒在歷史紀錄裡，幫它建立陣列
        if hist_key not in node_hist:
            node_hist[hist_key] = []

        # --- 存入數據與時間 ---
        tw_time = datetime.utcnow() + timedelta(hours=8)
        current_time = tw_time.strftime("%H:%M:%S")

        node_hist[hist_key].append(num_val)
        
        # 為了確保 labels 長度跟數據一致，每次 update 都檢查是否需要加標籤
        # 以該 node 收到的任何數據作為時間基準
        if len(node_hist["labels"]) < len(node_hist[hist_key]):
            node_hist["labels"].append(current_time)

        # --- 統一限制長度 ---
        max_len = 100
        for key in node_hist.keys():
            while len(node_hist[key]) > max_len:
                node_hist[key].pop(0)

        return {"status": "success"}, 200
    
    return {"status": "error"}, 400

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)