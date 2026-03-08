import serial
import requests
import time

# --- 1. 配置區域 ---
COM_PORT = 'COM3'         # 接收端 Arduino 的序列埠
BAUD_RATE = 9600
RENDER_URL = "https://ysz.onrender.com/update"
NODE_COOLDOWN = 3         # 同一節點上傳冷卻時間(秒)，避免過於頻繁

# 紀錄各節點上次更新時間
last_update = {}

# --- 2. 啟動序列埠連線 ---
try:
    ser = serial.Serial(COM_PORT, BAUD_RATE, timeout=1)
    print(f"✅ 成功連線至接收端 {COM_PORT}")
    print(f"📡 監聽中，準備轉發數據至：{RENDER_URL}")
except Exception as e:
    print(f"❌ 無法開啟序列埠: {e}")
    print("💡 請確認 COM Port 編號是否正確，且序列埠監視器已關閉。")
    exit()

# --- 3. 主循環 ---
while True:
    if ser.in_waiting > 0:
        try:
            # 讀取一行數據並清理
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if not line:
                continue

            print(f"📡 收到原始數據: {line}")

            # 邏輯 A：檢查是否為標準格式數據
            if "數據:" in line:
                # 只擷取「數據:」之後的內容
                # 範例：s02_t,21.5,h,80.3,req_time
                clean_data = line.split("數據:")[1].strip().lower()
                parts = clean_data.split(",")

                # 確保封包完整度 (ID, 溫, 標籤, 濕)
                if len(parts) >= 4:
                    node_id_base = parts[0].split('_')[0]
                    temp_val = parts[1]
                    hum_val = parts[3]

                    # 1. 處理校時請求
                    if "req_time" in clean_data:
                        # 取得當前電腦精準時間 (例如 TIME:15:05:30)
                        pc_time = time.strftime("TIME:%H:%M:%S")
                        # 透過序列埠傳回給接收端 Arduino，再由 LoRa 射回給發送端
                        ser.write((pc_time + "\n").encode())
                        print(f"⏰ 已回應校時請求: {pc_time}")

                    # 2. 雲端轉發檢查 (防暴衝)
                    now = time.time()
                    if node_id_base in last_update and (now - last_update[node_id_base] < NODE_COOLDOWN):
                        continue

                    # 3. 發送數據至 Render
                    t_payload = {"id": f"{node_id_base}_t", "val": temp_val}
                    h_payload = {"id": f"{node_id_base}_h", "val": hum_val}

                    try:
                        # 設定 5 秒超時，避免網路不穩導致整個 Bridge 卡死
                        r_t = requests.post(RENDER_URL, json=t_payload, timeout=5)
                        r_h = requests.post(RENDER_URL, json=h_payload, timeout=5)

                        if r_t.status_code == 200 and r_h.status_code == 200:
                            print(f"🚀 成功轉發 {node_id_base}：溫 {temp_val} / 濕 {hum_val}")
                        else:
                            print(f"❌ 雲端拒絕更新，狀態碼: {r_t.status_code}")
                    except requests.exceptions.RequestException as req_e:
                        print(f"🌐 網路連線異常，無法連上 Render: {req_e}")

                    last_update[node_id_base] = now
            
            # 邏輯 B：如果不包含「數據:」但包含其他重要資訊（選用）
            else:
                pass 

        except Exception as e:
            print(f"⚠️ 解析發生錯誤: {e}")

    # 稍微休息，避免 100% 佔用 CPU
    time.sleep(0.01)