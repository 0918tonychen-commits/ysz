import serial
import requests
import time

# --- 1. 配置區域 ---
COM_PORT = 'COM4'         # 請確認接收端 Arduino 的序列埠編號
BAUD_RATE = 115200        # 已全面提升至 115200
RENDER_URL = "https://ysz.onrender.com/update" # 雲端 API
NODE_COOLDOWN = 3         # 同一節點上傳冷卻時間(秒)，防止重複數據塞爆雲端

# 紀錄各節點上次更新時間
last_update = {}

# --- 2. 啟動序列埠連線 ---
try:
    # timeout 設為 0.1 讓讀取更即時，不阻塞 loop
    ser = serial.Serial(COM_PORT, BAUD_RATE, timeout=0.1)
    print(f"✅ 成功連線至接收端 {COM_PORT}")
    print(f"📡 監聽中（精簡封包模式），準備轉發數據至：{RENDER_URL}")
except Exception as e:
    print(f"❌ 無法開啟序列埠: {e}")
    print("💡 請確認 COM Port 編號是否正確，且序列埠監視器（Serial Monitor）已關閉。")
    exit()

# --- 3. 主循環 ---
while True:
    if ser.in_waiting > 0:
        try:
            # 讀取並解碼，忽略通訊干擾造成的損壞字元
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if not line: continue
            
            print(f"📥 原始數據: {line}")

            # 檢查是否包含關鍵標籤「數據:」
            if "數據:" in line:
                # 取得數據部分，例如: s02_t,25.6,h,72.8,c,651 (已移除 req_time)
                clean_data = line.split("數據:")[1].strip().lower()
                parts = clean_data.split(",") 
                
                if len(parts) >= 2:
                    # 抓取基礎節點 ID (如 s02 或 s03)
                    node_id_base = parts[0].split('_')[0] 
                    
                    # 防暴衝檢查：確保同一個節點不會在 3 秒內連續上傳
                    now = time.time()
                    if node_id_base in last_update and (now - last_update[node_id_base] < NODE_COOLDOWN):
                        continue

                    # --- 核心解析：遍歷所有數據對 ---
                    # 採兩兩一組的方式解析 (ID, 數值)
                    for i in range(0, len(parts) - 1, 2):
                        sensor_id = parts[i]
                        sensor_val = parts[i+1]
                        
                        # 過濾掉可能殘留的 req 標籤 (保險起見)
                        if "req" in sensor_id or "time" in sensor_id:
                            continue
                            
                        # 如果 ID 沒帶底線 (如單純傳 t, h, c), 自動補上 node_id 前綴
                        final_id = sensor_id if "_" in sensor_id else f"{node_id_base}_{sensor_id}"
                        
                        payload = {"id": final_id, "val": sensor_val}
                        
                        # --- 執行雲端上傳 ---
                        try:
                            # 加入 timeout=2，避免 Render 免費方案喚醒慢時卡住程式
                            res = requests.post(RENDER_URL, json=payload, timeout=2)
                            if res.status_code == 200:
                                print(f"🚀 成功上傳: {final_id} = {sensor_val}")
                            else:
                                print(f"⚠️ 雲端拒絕: {res.status_code}")
                        except Exception as upload_err:
                            print(f"❌ 網路傳輸失敗: {upload_err}")

                    # 更新該節點最後上傳時間
                    last_update[node_id_base] = now
                    
        except Exception as e:
            print(f"⚠️ 資料解析出錯: {e}")
            
    # 微小延遲，釋放 CPU 資源
    time.sleep(0.01)