import serial
import requests
import time

COM_PORT = 'COM4' # 請確認您的接收端 COM 埠
RENDER_URL = "https://ysz.onrender.com/update"
last_update = {} # 紀錄各 ID 上次更新時間，防止暴衝

ser = serial.Serial(COM_PORT, 9600, timeout=1)

while True:
    if ser.in_waiting > 0:
        try:
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            
            # 格式檢查: S01,25.5,64.0
            if "S0" in line and "," in line:
                parts = line.replace("內容: ", "").split(",")
                if len(parts) >= 3:
                    node_id = parts[0].lower() # 轉成 s01
                    temp = parts[1]
                    hum = parts[2]

                    # 防暴衝：同一節點 3 秒內只准上傳一次
                    now = time.time()
                    if node_id in last_update and (now - last_update[node_id] < 3):
                        continue

                    # 轉發至雲端：分別傳送溫度(_t)與濕度(_h)
                    requests.post(RENDER_URL, json={"id": f"{node_id}_t", "val": temp})
                    requests.post(RENDER_URL, json={"id": f"{node_id}_h", "val": hum})
                    
                    last_update[node_id] = now
                    print(f"成功轉發 {node_id} 數據: {temp}, {hum}")
        except Exception as e:
            print(f"解析錯誤: {e}")