import serial
import requests
import time

# --- 設定區 ---
COM_PORT = 'COM3'  # 請確認這是您「接收端」的 COM 埠
RENDER_URL = "https://ysz.onrender.com/update"
last_update = {}   # 紀錄各 ID 上次更新時間

try:
    ser = serial.Serial(COM_PORT, 9600, timeout=1)
    print(f"✅ 成功連線至接收端 {COM_PORT}")
except Exception as e:
    print(f"❌ 無法開啟序列埠: {e}")
    exit()

while True:
    if ser.in_waiting > 0:
        try:
            # 讀取數據並清理
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if not line:
                continue

            print(f"📡 收到原始數據: {line}")

            # 解析邏輯：預期格式為 s01_t,26.5,h,55.0
            if "s0" in line.lower() and "," in line:
                parts = line.split(",")
                
                # 確保封包完整性 (至少要有 ID, 溫值, h標籤, 濕值)
                if len(parts) >= 4:
                    node_id_base = parts[0].split('_')[0] # 抓取 s01
                    temp = parts[1]
                    hum = parts[3]

                    now = time.time()
                    # 防暴衝檢查
                    if node_id_base in last_update and (now - last_update[node_id_base] < 3):
                        continue

                    # 轉發至雲端：分別傳送溫度(_t)與濕度(_h)
                    t_payload = {"id": f"{node_id_base}_t", "val": temp}
                    h_payload = {"id": f"{node_id_base}_h", "val": hum}
                    
                    requests.post(RENDER_URL, json=t_payload)
                    requests.post(RENDER_URL, json=h_payload)
                    
                    last_update[node_id_base] = now
                    print(f"🚀 成功轉發 {node_id_base}：溫 {temp} / 濕 {hum}")

        except Exception as e:
            print(f"⚠️ 解析錯誤: {e}")
            
    time.sleep(0.1) # 稍微減輕 CPU 負擔