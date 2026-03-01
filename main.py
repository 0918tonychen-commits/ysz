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
    
    # --- 關鍵修改 1：放寬離線判定門檻 ---
    # 因為發送端 10 秒傳一次，加上傳輸延遲，建議門檻設為 30~40 秒
    # 若未來改為 5 分鐘傳一次，這裡請改為 360 秒
    for ts in last_seen_map.values():
        if now - ts < 40: 
            system_status = "online"
            break
    
    return jsonify({
        **all_sensors,
        "status": system_status,
        "history": history_data
    })

@app.route('/update', methods=['POST'])
def update():
    global all_sensors, history_data, last_seen_map
    data = request.json 
    
    if data and "id" in data:
        raw_id = data["id"].lower() 
        val_str = str(data.get("val", "0"))
        
        # --- 核心邏輯：數值精度控制 ---
        try:
            raw_num = float(val_str.replace("°C", "").replace("%", "").strip())
            num_val = round(raw_num, 1) 
        except (ValueError, TypeError):
            num_val = 0.0

        # 更新即時數據
        all_sensors[raw_id] = {"val": str(num_val)}
        
        # 解析節點資訊
        # 預期 ID 格式如: s01_t 或 s01_h
        parts = raw_id.split("_")
        node_id = parts[0]
        data_type = parts[1] if len(parts) > 1 else "t"
        
        # 更新最後看到該節點的時間
        last_seen_map[node_id] = time.time() 

        # 動態建立歷史空間
        if node_id not in history_data:
            history_data[node_id] = {"temp": [], "hum": [], "labels": []}

        node_hist = history_data[node_id]
        current_time = time.strftime("%H:%M:%S")

        # --- 數據存入歷史紀錄 (限制 100 筆) ---
        if data_type == "t": # 溫度
            node_hist["temp"].append(num_val)
            node_hist["labels"].append(current_time)
        elif data_type == "h": # 濕度
            node_hist["hum"].append(num_val)

        # 限制長度，防止記憶體溢出
        if len(node_hist["temp"]) > 100:
            node_hist["temp"].pop(0)
            node_hist["labels"].pop(0)
        if len(node_hist["hum"]) > 100:
            node_hist["hum"].pop(0)

        return {"status": "success"}, 200
    
    return {"status": "error"}, 400

if __name__ == '__main__':
    # 這裡 port 10000 是為了配合 Render 部署
    app.run(host='0.0.0.0', port=10000)