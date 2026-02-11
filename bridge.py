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
                
                # 確保收到的是「溫度,濕度」格式
                values = line.split(",")
                
                if len(values) >= 2:
                    try:
                        # --- 核心改進：確認數據是否為有效數字 ---
                        # 先轉成 float 再轉回字串，可以過濾掉非數字的亂碼
                        temp = str(float(values[0]))
                        hum = str(float(values[1]))
                        
                        # 同步到雲端
                        # 我們維持你原本的格式，讓網頁顯示 "XX.X °C"
                        requests.post(RENDER_URL, json={"id": "s01", "val": temp + " °C"})
                        requests.post(RENDER_URL, json={"id": "s02", "val": hum + " %"})
                        
                        print(f">>> 已同步雲端 - 溫度: {temp}, 濕度: {hum}")
                    
                    except ValueError:
                        # 如果 values[0] 或 values[1] 不是數字，會跳到這裡
                        print(f"收到無效數據內容 (可能是無線干擾): {line}")
                else:
                    print(f"數據格式不全 (收到: {line})，略過...")
                    
        except Exception as e:
            print(f"解析過程出錯: {e}")
    
    # --- 反應速度優化 ---
    # 將等待時間改為 0.1 秒，讓程式能更快捕捉到序列埠的數據
    time.sleep(0.1)