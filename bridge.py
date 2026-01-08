import serial
import requests
import time

# --- 設定區 ---
COM_PORT = 'COM3' 
BAUD_RATE = 9600
RENDER_URL = "https://ysz.onrender.com/update"

try:
    ser = serial.Serial(COM_PORT, BAUD_RATE, timeout=1)
    print(f"成功連接 {COM_PORT}，開始編號轉發...")
except Exception as e:
    print(f"失敗: {e}")
    exit()

while True:
    if ser.in_waiting > 0:
        try:
            line = ser.readline().decode('utf-8').strip()
            print(f"收到數據: {line}") # 假設收到 "23.15,65.82"

            values = line.split(",")
            
            if len(values) >= 2:
                # 1. 發送溫度到 s01
                requests.post(RENDER_URL, json={"id": "s01", "val": values[0] + " °C"})
                # 2. 發送濕度到 s02
                requests.post(RENDER_URL, json={"id": "s02", "val": values[1] + " %"})
                
                print(f"已更新 s01:{values[0]} 和 s02:{values[1]}")
            
        except Exception as e:
            print(f"處理失敗: {e}")
    
    time.sleep(1)