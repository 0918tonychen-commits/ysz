import serial
import requests
import time

# --- 設定區 ---
COM_PORT = 'COM4'  # 請確認您的接收板在哪個 COM 埠
BAUD_RATE = 9600
RENDER_URL = "https://ysz.onrender.com/update"

try:
    ser = serial.Serial(COM_PORT, BAUD_RATE, timeout=1)
    print(f"成功連接 {COM_PORT}，LoRa 電腦轉發站啟動中...")
except Exception as e:
    print(f"錯誤: 無法開啟序列埠 {e}")
    exit()

while True:
    if ser.in_waiting > 0:
        try:
            # 讀取接收端板子印出的內容
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            
            # 1. 過濾掉顯示用的分隔線或提示文字
            # 我們只處理包含 "T:" 的數據行
            if "T:" in line and "H:" in line:
                print(f"收到有效數據行: {line}")
                
                # 2. 解析標籤格式 (範例: T:24.8,H:69.0,Count:10)
                parts = line.split(",")
                
                # 擷取 T: 之後的數字
                temp = parts[0].split(":")[1]
                # 擷取 H: 之後的數字
                hum = parts[1].split(":")[1]
                
                # 3. 同步到 Render 雲端
                # 維持您網頁原本的 ID 對應
                requests.post(RENDER_URL, json={"id": "s01", "val": temp + " °C"})
                requests.post(RENDER_URL, json={"id": "s02", "val": hum + " %"})
                
                print(f">>> 網頁已更新 - 溫度: {temp}, 濕度: {hum}")
            
            # 如果是 RSSI 或其他除錯訊息，只印在終端機不傳雲端
            elif "RSSI" in line:
                print(f"訊號品質檢查: {line}")

        except Exception as e:
            print(f"解析過程出錯: {e}")
    
    # 維持 0.1 秒檢查一次，反應速度最快
    time.sleep(0.1)
