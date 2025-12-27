from flask import Flask
import serial
import time

app = Flask(__name__)

# 設定 Arduino 連接 (請確認 COM port)
try:
    ser = serial.Serial('COM5', 9600, timeout=2)
    time.sleep(2) # 等待 Arduino 重啟
except Exception as e:
    print(f"無法連接序列埠: {e}")

@app.route('/')
def index():
    temp = "N/A"
    hum = "N/A"
    
    try:
        # 核心優化：清空舊緩衝區，抓取最新資料
        ser.reset_input_buffer()
        
        # 讀取一行並解碼
        line = ser.readline().decode('utf-8').strip()
        
        # 檢查是否包含逗號 (確保是我們想要的數據格式)
        if "," in line:
            data = line.split(',')
            if len(data) == 2:
                temp = data[0]
                hum = data[1]
        else:
            # 如果第一行沒抓到(可能是剛啟動的文字)，再嘗試讀一次
            line = ser.readline().decode('utf-8').strip()
            if "," in line:
                data = line.split(',')
                temp, hum = data[0], data[1]

    except Exception as e:
        print(f"讀取錯誤: {e}")
        temp, hum = "Error", "Error"

    return f"""
    <html>
    <head>
        <title>Arduino MKR 1310 SHT31</title>
        <meta http-equiv="refresh" content="2">
        <style>
            body {{ font-family: 'Segoe UI', Arial, sans-serif; text-align: center; margin-top: 50px; background-color: #f4f4f9; }}
            .container {{ display: inline-block; padding: 20px; background: white; border-radius: 15px; shadow: 0 4px 6px rgba(0,0,0,0.1); }}
            h1 {{ color: #333; }}
            .data {{ font-size: 24px; color: #007bff; font-weight: bold; }}
            .label {{ color: #555; margin-top: 10px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Arduino SHT31 監測站</h1>
            <div class="label">當前溫度</div>
            <div class="data">{temp} °C</div>
            <hr>
            <div class="label">當前濕度</div>
            <div class="data">{hum} %</div>
        </div>
    </body>
    </html>
    """

if __name__ == '__main__':
    # host='0.0.0.0' 允許同區域網路的其他裝置存取
    app.run(host='0.0.0.0', port=5000, debug=False)
    