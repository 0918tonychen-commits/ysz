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
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if not line: continue

            print(f"📡 收到原始數據: {line}")

            # --- 關鍵修正：只抓取「數據: 」之後的內容 ---
            if "數據:" in line:
                clean_data = line.split("數據:")[1].strip().lower() # 取得 s02_t,21.5,h,80.3
                parts = clean_data.split(",")
                
                # 確保格式正確 (ID, 溫, h標籤, 濕)
                if len(parts) >= 4:
                    node_id_base = parts[0].split('_')[0] # 抓取 s01 或 s02
                    temp = parts[1]
                    hum = parts[3]

                    now = time.time()
                    # 防暴衝 (3秒內不重複傳送同一節點)
                    if node_id_base in last_update and (now - last_update[node_id_base] < 3):
                        continue

                    # 轉發至雲端
                    t_payload = {"id": f"{node_id_base}_t", "val": temp}
                    h_payload = {"id": f"{node_id_base}_h", "val": hum}
                    
                    try:
                        r_t = requests.post(RENDER_URL, json=t_payload, timeout=5)
                        r_h = requests.post(RENDER_URL, json=h_payload, timeout=5)
                        
                        if r_t.status_code == 200:
                            print(f"🚀 成功轉發 {node_id_base}：溫 {temp} / 濕 {hum}")
                        else:
                            print(f"❌ 雲端拒絕: {r_t.status_code}")
                    except Exception as req_e:
                        print(f"🌐 網路連線失敗: {req_e}")
                    
                    last_update[node_id_base] = now
            else:
                print("⚠️ 格式不符，跳過解析")

        except Exception as e:
            print(f"⚠️ 解析錯誤: {e}")
            
    time.sleep(0.1) # 降低 CPU 使用率