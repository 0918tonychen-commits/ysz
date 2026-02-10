from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

# 1. 建立編號對應表 (通訊錄)
NAME_MAP = {
    "s01": "環境溫度",
    "s02": "環境濕度"
}

# 2. 初始數據
sensor_data = {
    "s01": "24.8", # 先給予預設值方便測試
    "s02": "69.0"
}

@app.route('/')
def index():
    # 這是原本的首頁路徑
    return render_template('index.html', sensors=sensor_data, names=NAME_MAP)

# --- 新增這個路徑給 JavaScript 抓取數據 ---
@app.route('/api/data')
def get_data():
    # 回傳 JSON 格式的數據
    return jsonify({
        "temperature": sensor_data["s01"],
        "humidity": sensor_data["s02"],
        "status": "connected"
    })

@app.route('/update', methods=['POST'])
def update():
    global sensor_data
    data = request.json 
    if data and "id" in data:
        sensor_id = data["id"]
        # 更新內部數據
        sensor_data[sensor_id] = data.get("val", "N/A")
        return {"status": "success"}, 200
    return {"status": "error"}, 400

if __name__ == '__main__':
    # Render 部署需要監聽 0.0.0.0 和指定的 Port
    app.run(host='0.0.0.0', port=10000)
    