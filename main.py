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
    for ts in last_seen_map.values():
        if now - ts < 20:
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
        
        # --- 核心修改 1：強制控制數值精度 ---
        try:
            # 清理字串並轉換為浮點數，四捨五入到第 1 位
            raw_num = float(val_str.replace("°C", "").replace("%", "").strip())
            num_val = round(raw_num, 1) # 強制控制小數點
        except (ValueError, TypeError):
            num_val = 0.0

        # 更新即時數據 (使用精簡後的數字)
        all_sensors[raw_id] = {"val": str(num_val)}
        
        # 解析節點資訊
        node_id = raw_id.split("_")[0]
        data_type = raw_id.split("_")[1] if "_" in raw_id else "t"
        last_seen_map[node_id] = time.time() 

        # 動態建立歷史空間
        if node_id not in history_data:
            history_data[node_id] = {"temp": [], "hum": [], "labels": []}

        node_hist = history_data[node_id]
        current_time = time.strftime("%H:%M:%S")

        # --- 核心修改 2：存入精簡數值並限制 100 筆 ---
        if data_type == "t": # 溫度
            node_hist["temp"].append(num_val)
            node_hist["labels"].append(current_time)
        elif data_type == "h": # 濕度
            node_hist["hum"].append(num_val)

        # 限制長度
        if len(node_hist["temp"]) > 100:
            node_hist["temp"].pop(0)
            node_hist["labels"].pop(0)
        if len(node_hist["hum"]) > 100:
            node_hist["hum"].pop(0)

        return {"status": "success"}, 200
    
    return {"status": "error"}, 400

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)