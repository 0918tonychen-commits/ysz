import serial
import requests
import time

# --- 設定區 ---
COM_PORT = 'COM3'  # 根據你截圖中成功的 COM3 填寫
BAUD_RATE = 9600
RENDER_URL = "https://ysz.onrender.com/update"

try:
    ser = serial.Serial(COM_PORT, BAUD_RATE, timeout=1)
    print(f"成功連接 {COM_PORT}，開始轉發數據...")
except Exception as e:
    print(f"無法開啟序列埠: {e}")
    exit()

while True:
    if ser.in_waiting > 0:
        try:
            # 讀取數據，例如 "23.15,65.82"
            line = ser.readline().decode('utf-8').strip()
            print(f"收到原始數據: {line}")

            # 將字串依逗號拆分成清單
            values = line.split(",")
            
            # 確保收到兩個數值才發送
            if len(values) >= 2:
                payload = {
                    "temp": values[0], # 第一個是溫度
                    "hum": values[1],  # 第二個是濕度
                    "rssi": "N/A"
                }
                
                # 發送到雲端
                response = requests.post(RENDER_URL, json=payload)
                print(f"同步至雲端狀態: {response.status_code}")
            else:
                print("格式不符，略過數據")

        except Exception as e:
            print(f"處理失敗: {e}")
    
    time.sleep(1)
