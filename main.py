from flask import Flask, render_template, request

app = Flask(__name__)

# 用來儲存最新數據的變數
latest_data = {"temp": "N/A", "hum": "N/A", "rssi": "N/A"}

@app.route('/')
def index():
    # 顯示網頁，並把最新數據丟進去
    return render_template('index.html', data=latest_data)

@app.route('/update', methods=['POST'])
def update():
    global latest_data
    # 接收來自你家電腦發送的 POST 請求
    data = request.json
    if data:
        latest_data["temp"] = data.get("temp", "N/A")
        latest_data["hum"] = data.get("hum", "N/A")
        latest_data["rssi"] = data.get("rssi", "N/A")
        return {"status": "success"}, 200
    return {"status": "error"}, 400

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)