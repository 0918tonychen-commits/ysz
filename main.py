from flask import Flask, render_template, request, jsonify
from datetime import datetime, timedelta
import time

app = Flask(__name__)

# --- 1. 配置區：感測器單位映射表 ---
# 這裡定義了各類感測器的單位，前端 index.html 會自動讀取並顯示
UNIT_MAP = {
    "t": "°C",      # 溫度
    "h": "%",       # 濕度
    "co2": "ppm",   # 二氧化碳
    "voc": "mg/m³", # 揮發性有機物 (甲苯等)
    "vib": "g",     # 震動強度 (或用 Hz 表示頻率)
    "v": "V"        # 電池電壓
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
        "units": UNIT_MAP  # 將單位映射表傳給前端，減少前端手寫邏輯
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
            num_val = round(raw_num, 2) # 震動數值通常建議保留兩位小數
        except (ValueError, TypeError):
            num_val = 0.0

        all_sensors[raw_id] = {"val": str(num_val)}
        
        # 解析 ID (例如 s01_vib -> node_id: s01, data_type: vib)
        parts = raw_id.split("_")
        node_id = parts[0]
        data_type = parts[1] if len(parts) > 1 else "t"
        
        last_seen_map[node_id] = time.time() 

        # 動態建立該節點的歷史結構
        if node_id not in history_data:
            history_data[node_id] = {
                "temp": [], "hum": [], "co2": [], "voc": [], "vib": [], "labels": []
            }

        node_hist = history_data[node_id]
        tw_time = datetime.utcnow() + timedelta(hours=8)
        current_time = tw_time.strftime("%H:%M:%S")

        # --- 根據數據類型存入對應陣列 ---
        if data_type == "t": 
            node_hist["temp"].append(num_val)
            node_hist["labels"].append(current_time) # 標籤隨主數據更新
        elif data_type == "h": 
            node_hist["hum"].append(num_val)
        elif data_type == "co2":
            node_hist["co2"].append(num_val)
        elif data_type == "voc":
            node_hist["voc"].append(num_val)
        elif data_type == "vib":
            node_hist["vib"].append(num_val) # 存入震動歷史

        # 統一限制長度，確保 labels 與各數值陣列同步
        max_len = 100
        for key in node_hist.keys():
            while len(node_hist[key]) > max_len:
                node_hist[key].pop(0)

        return {"status": "success"}, 200
    
    return {"status": "error"}, 400

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)