import serial
import requests
import time

# --- 1. 配置區域 ---
COM_PORT = 'COM6' 
BAUD_RATE = 115200 
RENDER_URL = "https://ysz.onrender.com/update"
NODE_COOLDOWN = 2 # 節點上傳冷卻

last_update = {}

try:
    ser = serial.Serial(COM_PORT, BAUD_RATE, timeout=0.1)
    print(f"✅ 成功連線至雙頻網關 {COM_PORT}")
    print(f"📡 正在接收頻道 B (923.4MHz) 的接力數據...")
except Exception as e:
    print(f"❌ 無法開啟序列埠: {e}")
    exit()

while True:
    if ser.in_waiting > 0:
        try:
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if not line or "數據:" not in line: continue
            
            print(f"📥 原始數據: {line}")

            # 取得「數據:」後方的內容，例如: L2,s02_M36_t,26.7,h,74.2...
            raw_payload = line.split("數據:")[1].strip()
            parts = raw_payload.split(",") 

            # 邏輯優化：找出這串數據的主節點 (s02 或 s03)
            # 因為 s02 是一個單一節點，會同時測量溫濕度與 CO2
            current_node = "s02" if "s02" in raw_payload.lower() else "s03"

            # 遍歷所有零件，尋找 (Key, Value) 對
            for i in range(len(parts)):
                item = parts[i]
                
                # 處理帶有底線的 Key (如 s02_M36_t, s03_pm25)
                if "_" in item and i + 1 < len(parts):
                    key = item
                    val = parts[i+1]
                    
                    # 處理特殊的 RSSI 標籤 (如 via_s02_RSSI:-55)
                    if ":" in key:
                        key, val = key.split(":")
                    
                    # 排除 L2 等非數據項
                    if key.upper() == "L2": continue

                    # 執行上傳
                    payload = {"id": key, "val": val}
                    try:
                        # 提高至 10 秒以應付 Render 喚醒
                        res = requests.post(RENDER_URL, json=payload, timeout=10)
                        if res.status_code == 200:
                            print(f"🚀 成功上傳: {key} = {val}")
                    except:
                        print(f"❌ Render 喚醒逾時，跳過此筆: {key}")

        except Exception as e:
            print(f"⚠️ 解析出錯: {e}")
            
    time.sleep(0.01)