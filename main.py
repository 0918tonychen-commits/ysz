from flask import Flask, render_template, request

app = Flask(__name__)

# 使用字典存儲，Key 是編號，Value 是數值
sensor_data = {
    "s01": "等待中...", # 預設給溫度
    "s02": "等待中..."  # 預設給濕度
}

@app.route('/')
def index():
    # 將整個字典傳給網頁
    return render_template('index.html', sensors=sensor_data)

@app.route('/update', methods=['POST'])
def update():
    global sensor_data
    data = request.json 
    # 接收格式: {"id": "s01", "val": "23.5"}
    if data and "id" in data:
        sensor_id = data["id"]
        sensor_data[sensor_id] = data.get("val", "N/A")
        return {"status": "success", "id": sensor_id}, 200
    return {"status": "error"}, 400

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)