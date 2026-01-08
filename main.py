from flask import Flask, render_template, request

app = Flask(__name__)

# 1. 建立編號對應表 (通訊錄)
NAME_MAP = {
    "s01": "環境溫度",
    "s02": "環境濕度"
}

# 2. 初始數據
sensor_data = {
    "s01": "等待中...",
    "s02": "等待中..."
}

@app.route('/')
def index():
    # 同時把數據和對應表傳給網頁
    return render_template('index.html', sensors=sensor_data, names=NAME_MAP)

@app.route('/update', methods=['POST'])
def update():
    global sensor_data
    data = request.json 
    if data and "id" in data:
        sensor_id = data["id"]
        sensor_data[sensor_id] = data.get("val", "N/A")
        return {"status": "success"}, 200
    return {"status": "error"}, 400

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)