import serial
import requests
import time

# --- 1. 配置區域 ---
COM_PORT = 'COM4'         # 請確認這仍是你的接收端 Port
BAUD_RATE = 115200        # 強烈建議改為 115200 (Arduino 端也要改)
RENDER_URL = "https://ysz.onrender.com/update"
NODE_COOLDOWN = 3         # 同一節點冷卻時間

# 紀錄各節點上次更新時間
last_update = {}

# --- 2. 啟動序列埠連線 ---
try:
    # 增加 timeout 讓讀取更即時
    ser = serial.Serial(COM_PORT, BAUD_RATE, timeout=0.1)
    print(f"✅ 成功連線至網關 {COM_PORT} (Baud: {BAUD_RATE})")
    print(f"📡 單向監聽模式啟動，轉發至：{RENDER_URL}")
except Exception as e:
    print(f"❌ 無法開啟序列埠: {e}")
    exit()

# --- 3. 主循環 ---
while True:
    if ser.in_waiting > 0:
        try:
            # 讀取並解碼，忽略損壞的字元
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if not line: continue
            
            # 在終端機顯示原始訊息，方便觀察 RSSI
            print(f"📥 原始數據: {line}")

            # 判斷是否包含關鍵數據標籤
            if "數據:" in line:
                # 取得「數據:」後面的內容：s02_t,25.4,h,60.1,c,550,req_time
                clean_data = line.split("數據:")[1].strip().lower()
                parts = clean_data.split(",") 
                
                if len(parts) >= 2:
                    # 抓取節點 ID (如 s02, s03)
                    node_id_base = parts[0].split('_')[0] 
                    
                    # 防暴衝檢查：避免 Render 被塞爆
                    now = time.time()
                    if node_id_base in last_update and (now - last_update[node_id_base] < NODE_COOLDOWN):
                        continue

                    # 遍歷所有數據對
                    for i in range(0, len(parts) - 1, 2):
                        sensor_id = parts[i]
                        sensor_val = parts[i+1]
                        
                        # 過濾掉 req_time 等非數值標籤
                        if "req" in sensor_id or "time" in sensor_id:
                            continue
                            
                        # 格式化 ID，例如 s02_t, s03_pm25
                        final_id = sensor_id if "_" in sensor_id else f"{node_id_base}_{sensor_id}"
                        
                        payload = {"id": final_id, "val": sensor_val}
                        
                        # --- 核心：執行雲端上傳 ---
                        try:
                            # timeout=2 防止網路慢卡死 Serial 讀取
                            res = requests.post(RENDER_URL, json=payload, timeout=2)
                            if res.status_code == 200:
                                print(f"🚀 成功上傳: {final_id} = {sensor_val}")
                            else:
                                print(f"⚠️ 雲端拒絕: {res.status_code}")
                        except Exception as upload_err:
                            print(f"❌ 網路傳輸失敗: {upload_err}")

                    # 更新該節點的最後上傳時間
                    last_update[node_id_base] = now
                    
        except Exception as e:
            print(f"⚠️ 處理過程出錯: {e}")
            
    time.sleep(0.01) # 微小延遲減少 CPU 負擔