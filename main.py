from flask import Flask, render_template, request, jsonify
import time

app = Flask(__name__)

# 1. 建立編號對應表
NAME_MAP = {
    "s01": "環境溫度",
    "s02": "環境濕度"
}

# 2. 初始數據
sensor_data = {
    "s01": "等待中",
    "s02": "等待中"
}

# --- 新增：紀錄歷史數據 ---
# 用來存最近 20 筆溫度與時間點
temp_history = []
hum_history = []
time_labels = []

last_seen = 0

@app.route('/')
def index():
    return render_template('index.html', sensors=sensor_data, names=NAME_MAP)

@app.get("/api/data")
def get_data():
    global last_seen
    seconds_ago = time.time() - last_seen
    is_online = "online" if seconds_ago < 15 else "offline"
    
    return jsonify({
        "temperature": sensor_data["s01"],
        "humidity": sensor_data["s02"],
        "status": is_online,
        "history": {
            "temp": temp_history,
            "hum": hum_history,
            "labels": time_labels
        }
    })

@app.route('/update', methods=['POST'])
def update():
    global sensor_data, last_seen, temp_history, hum_history, time_labels
    data = request.json 
    if data and "id" in data:
        sensor_id = data["id"]
        val_str = data.get("val", "0")
        sensor_data[sensor_id] = val_str
        last_seen = time.time() 

        # 數值處理：將 "25.5 °C" 轉為 25.5
        try:
            num_val = float(val_str.split(" ")[0])
            current_time = time.strftime("%H:%M:%S")

            if sensor_id == "s01": # 溫度
                temp_history.append(num_val)
                time_labels.append(current_time)
            elif sensor_id == "s02": # 濕度
                hum_history.append(num_val)

            # 限制長度，只保留最近 20 筆
            if len(temp_history) > 20:
                temp_history.pop(0)
                time_labels.pop(0)
            if len(hum_history) > 20:
                hum_history.pop(0)
        except:
            pass

        return {"status": "success"}, 200
    return {"status": "error"}, 400

if __name__ == '__main__':
    # Render 環境通常使用 10000 埠口 
    app.run(host='0.0.0.0', port=10000)