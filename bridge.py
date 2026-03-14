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
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if not line: continue
            print(f"📡 原始數據: {line}")

            if "數據:" in line:
                clean_data = line.split("數據:")[1].strip().lower()
                parts = clean_data.split(",") # 例如: s01_t,25.5,h,60.2,co2,450,req_time
                
                if len(parts) >= 2:
                    node_id_base = parts[0].split('_')[0] # 抓取 s01
                    
                    # 1. 處理校時請求
                    if "req_time" in clean_data:
                        pc_time = time.strftime("TIME:%H:%M:%S")
                        ser.write((pc_time + "\n").encode())
                        print(f"⏰ 已回應校時: {pc_time}")

                    # 2. 防暴衝檢查
                    now = time.time()
                    if node_id_base in last_update and (now - last_update[node_id_base] < NODE_COOLDOWN):
                        continue

                    # 3. 核心修改：自動遍歷所有數據對
                    # 我們每兩個一組 (ID, 數值) 進行處理
                    for i in range(0, len(parts) - 1, 2):
                        sensor_id = parts[i]
                        sensor_val = parts[i+1]
                        
                        # 跳過非數據標籤 (如 req_time)
                        if "req" in sensor_id or "time" in sensor_id:
                            continue
                            
                        # 如果 ID 沒帶底線 (如單純傳 co2), 自動補上 node_id
                        final_id = sensor_id if "_" in sensor_id else f"{node_id_base}_{sensor_id}"
                        
                        payload = {"id": final_id, "val": sensor_val}
                        
                        try:
                            res = requests.post(RENDER_URL, json=payload, timeout=5)
                            if res.status_code == 200:
                                print(f"🚀 成功上傳: {final_id} = {sensor_val}")
                        except:
                            print(f"❌ 上傳失敗: {final_id}")

                    last_update[node_id_base] = now
        except Exception as e:
            print(f"⚠️ 錯誤: {e}")
    time.sleep(0.01)