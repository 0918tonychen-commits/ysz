import serial
import requests
import time

# --- 設定區 ---
COM_PORT = 'COM3' 
BAUD_RATE = 9600
RENDER_URL = "https://ysz.onrender.com/update"

try:
    ser = serial.Serial(COM_PORT, BAUD_RATE, timeout=1)
    print(f"成功連接 {COM_PORT}，LoRa 無線轉發啟動中...")
except Exception as e:
    print(f"錯誤: 無法開啟序列埠 {e}")
    exit()

while True:
    if ser.in_waiting > 0:
        try:
            # 讀取 LoRa 接收端傳來的字串
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            
            if line:
                print(f"無線接收數據: {line}")
                
                # 確保收到的是「數字,數字」格式
                values = line.split(",")
                
                if len(values) >= 2:
                    # 嘗試將字串轉為數字，確認不是亂碼
                    temp = values[0]
                    hum = values[1]
                    
                    # 同步到雲端
                    requests.post(RENDER_URL, json={"id": "s01", "val": temp + " °C"})
                    requests.post(RENDER_URL, json={"id": "s02", "val": hum + " %"})
                    
                    print(f">>> 已同步雲端 - 溫度: {temp}, 濕度: {hum}")
                else:
                    print("數據格式不全，略過...")
                    
        except Exception as e:
            print(f"解析過程出錯 (可能是干擾): {e}")
    
    time.sleep(0.5) # 稍微縮短等待時間，反應更快