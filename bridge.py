import serial
import requests
import time

# --- 1. 設定區 ---
COM_PORT = 'COM3'  # 務必確認這是「接收端」的正確 COM 埠
BAUD_RATE = 9600
RENDER_URL = "https://ysz.onrender.com/update"
last_update = {}   # 紀錄各 ID 上次更新時間，防止數據過於頻繁

# --- 2. 啟動序列埠 ---
try:
    ser = serial.Serial(COM_PORT, BAUD_RATE, timeout=1)
    print(f"✅ 成功連線至接收端 {COM_PORT}")
except Exception as e:
    print(f"❌ 無法開啟序列埠: {e}")
    exit()

while True:
    if ser.in_waiting > 0:
        try:
            # 讀取並清理字串
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if not line:
                continue

            print(f"📡 收到原始數據: {line}")

            # 統一格式處理：將冒號轉為逗號，並轉小寫
            clean_line = line.replace(":", ",").lower()
            
            # 解析邏輯：預期格式如 s01_t,26.5,h,55.0 或 s02_t,21.6,h:79.9
            if "s0" in clean_line and "," in clean_line:
                parts = clean_line.split(",")
                
                # 確保封包長度足夠 (ID, 溫, 標籤, 濕)
                if len(parts) >= 4:
                    node_id_base = parts[0].split('_')[0] # 抓取 s01 或 s02
                    temp = parts[1]
                    hum = parts[3]

                    # 防暴衝：同一節點 3 秒內只准上傳一次
                    now = time.time()
                    if node_id_base in last_update and (now - last_update[node_id_base] < 3):
                        continue

                    # 3. 轉發至雲端
                    t_payload = {"id": f"{node_id_base}_t", "val": temp}
                    h_payload = {"id": f"{node_id_base}_h", "val": hum}
                    
                    try:
                        # 增加 timeout 防止網路卡住程式
                        r_t = requests.post(RENDER_URL, json=t_payload, timeout=5)
                        r_h = requests.post(RENDER_URL, json=h_payload, timeout=5)
                        
                        if r_t.status_code == 200:
                            print(f"🚀 雲端更新成功！{node_id_base}：溫 {temp} / 濕 {hum}")
                        else:
                            print(f"❌ 雲端拒絕數據！狀態碼: {r_t.status_code}")
                    except Exception as req_e:
                        print(f"🌐 網路連線異常: {req_e}")
                    
                    last_update[node_id_base] = now

        except Exception as e:
            print(f"⚠️ 解析錯誤: {e}")
            
    time.sleep(0.1) # 降低 CPU 使用率