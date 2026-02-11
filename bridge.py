import serial
import requests
import time

# --- 設定區 ---
COM_PORT = 'COM10'  # 請確認您的接收板在哪個 COM 埠
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
            
            # 1. 處理包含溫濕度數據的行
            if "T:" in line and "H:" in line:
                # --- 關鍵修正：先移除「內容: 」這幾個字，避免解析錯誤 ---
                clean_line = line.replace("內容: ", "")
                print(f"收到有效數據行: {clean_line}")
                
                # 2. 解析標籤格式 (範例: T:21.3,H:63.9,Count:327)
                parts = clean_line.split(",")
                
                # 擷取 T: 之後的數字
                # 使用 split(":")[-1] 確保只抓到冒號後面的數值
                temp = parts[0].split(":")[-1].strip()
                # 擷取 H: 之後的數字
                hum = parts[1].split(":")[-1].strip()
                
                # 3. 同步到 Render 雲端
                requests.post(RENDER_URL, json={"id": "s01", "val": temp + " °C"})
                requests.post(RENDER_URL, json={"id": "s02", "val": hum + " %"})
                
                print(f">>> 網頁已更新 - 溫度: {temp}, 濕度: {hum}")
            
            # 處理訊號品質訊息
            elif "RSSI" in line:
                print(f"訊號品質檢查: {line}")

        except Exception as e:
            print(f"解析過程出錯: {e}")
    
    # 維持高頻率檢查，反應速度最快
    time.sleep(0.1)
