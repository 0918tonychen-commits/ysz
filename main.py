from flask import Flask, render_template, request, jsonify
import time  # 導入時間模組以計算超時

app = Flask(__name__)

# 1. 建立編號對應表 (通訊錄)
NAME_MAP = {
    "s01": "環境溫度",
    "s02": "環境濕度"
}

# 2. 初始數據
sensor_data = {
    "s01": "等待中",
    "s02": "等待中"
}

# 3. 紀錄最後一次收到數據的時間 (伺服器啟動時先設為 0)
last_seen = 0

@app.route('/')
def index():
    return render_template('index.html', sensors=sensor_data, names=NAME_MAP)

# --- 修改後的 API 路徑：增加在線狀態判斷 ---
@app.get("/api/data")
def get_data():
    global last_seen
    # 計算距離現在幾秒沒更新
    seconds_ago = time.time() - last_seen
    
    # 邏輯：如果 10 秒內有收到 update 請求則在線，否則離線
    # (注意：伺服器剛重啟時會顯示離線，直到收到第一筆數據)
    is_online = "online" if seconds_ago < 10 else "offline"
    
    return jsonify({
        "temperature": sensor_data["s01"],
        "humidity": sensor_data["s02"],
        "status": is_online  # 將真實連線狀態傳給網頁
    })

@app.route('/update', methods=['POST'])
def update():
    global sensor_data, last_seen
    data = request.json 
    if data and "id" in data:
        sensor_id = data["id"]
        sensor_data[sensor_id] = data.get("val", "N/A")
        # 關鍵點：每次收到硬體 POST 數據，就更新時間戳記
        last_seen = time.time() 
        return {"status": "success"}, 200
    return {"status": "error"}, 400

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)