import serial
import requests
import time
import re

# ==========================================
# 1. 配置區域
# ==========================================
COM_PORT = 'COM4'           # 請根據實際情況修改 COM Port
BAUD_RATE = 115200          # 必須與 Arduino 網關端一致
RENDER_URL = "https://ysz.onrender.com/update"
NODE_COOLDOWN = 1           # 節點上傳冷卻時間 (秒)

# 定義目標感測器標籤
VALID_SENSORS = ['t', 'h', 'c', 'pm25', 'pm10']

# 紀錄各節點上次更新時間
last_update = {}

# ==========================================
# 2. 核心解析函數 (萬能關鍵字過濾 + 批次打包)
# ==========================================
def extract_as_batch(raw_str):
    """
    邏輯：
    1. 利用正則表達式自動偵測來源節點 (s02 或 s03)
    2. 掃描所有逗號分隔項，只抓取 VALID_SENSORS 定義的物理量
    3. 自動排除流水號 (_Mxxx_) 與層級標籤 (L2, L3)
    """
    # 自動識別主節點 (s02 或 s03)
    node_match = re.search(r'(s0\d)', raw_str.lower())
    current_node = node_match.group(1) if node_match else "unknown"
    
    parts = raw_str.split(',')
    batch_data = {} 

    for i in range(len(parts)):
        item = parts[i].strip().lower()
        
        for sensor in VALID_SENSORS:
            # 匹配規則：項目結尾是感測器標籤 (如 s02_m149_t 匹配 t)
            if item.endswith(sensor) and i + 1 < len(parts):
                val = parts[i+1].strip()
                
                # 確認後方接著的是合法數字
                if re.match(r'^-?\d+(\.\d+)?$', val):
                    # 存入字典，例如 {"t": "26.2", "h": "71.0"}
                    batch_data[sensor] = val
                    break 
                    
    return current_node, batch_data

# ==========================================
# 3. 序列埠啟動
# ==========================================
try:
    ser = serial.Serial(COM_PORT, BAUD_RATE, timeout=0.1)
    print(f"✅ 成功連線至雙頻網關 {COM_PORT}")
    print(f"📡 批次上傳模式啟動，目標：{RENDER_URL}")
except Exception as e:
    print(f"❌ 無法開啟序列埠: {e}")
    exit()

# ==========================================
# 4. 主循環 (監聽、解析、上傳)
# ==========================================
while True:
    if ser.in_waiting > 0:
        try:
            # 讀取並清除前後空白
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if not line or "數據:" not in line: 
                continue
            
            # 取得「數據:」後方的內容
            payload_str = line.split("數據:")[1].strip()
            print(f"📥 收到原始數據: {payload_str}")

            # 執行批次解析
            node_id, data_package = extract_as_batch(payload_str)

            if data_package:
                now = time.time()
                
                # 防暴衝冷卻檢查
                if node_id in last_update and (now - last_update[node_id] < NODE_COOLDOWN):
                    continue

                # 封裝為批次 JSON 格式
                payload = {
                    "node": node_id,
                    "data": data_package,
                    "timestamp": int(now)
                }
                
                try:
                    # 發送一次 POST 請求，包含所有 sensor 數據
                    # Timeout 設為 10 秒應付 Render 喚醒
                    res = requests.post(RENDER_URL, json=payload, timeout=10)
                    
                    if res.status_code == 200:
                        print(f"🚀 [同步上傳成功] 節點 {node_id}: {data_package}")
                        last_update[node_id] = now
                    else:
                        print(f"⚠️ 雲端拒絕 (HTTP {res.status_code})")
                        
                except Exception as net_err:
                    print(f"❌ 網路逾時或中斷，正在等待 Render 響應...")

        except Exception as e:
            print(f"⚠️ 系統異常: {e}")
            
    # 微小延遲避免 100% 佔用 CPU
    time.sleep(0.01)