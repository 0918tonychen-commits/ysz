import serial
import requests
import time
import re

# --- 配置區 ---
COM_PORT = 'COM6' 
BAUD_RATE = 115200 
RENDER_URL = "https://ysz.onrender.com/update"
# 系統支持的所有感測器標籤
VALID_SENSORS = ['t', 'h', 'c', 'pm25', 'pm10', 'v', 'p', 'lux']

def extract_universal(raw_str):
    """
    升級版智慧解析：支援動態路由標頭 (Target, Level, Source_Mcount)
    """
    parts = raw_str.split(',')
    batch_data = {} 
    current_node = "unknown"

    # 1. 識別真實數據源頭 (鎖定帶有 _m 特徵的發件人標籤)
    for item in parts:
        item_low = item.strip().lower()
        # 新版路由特徵：真正的發件人會帶有 _m (例如 s03_m12)
        if "s0" in item_low and "_m" in item_low:
            match = re.search(r'(s\d+)', item_low)
            if match:
                current_node = match.group(1)
                break
                
    # (向下相容機制：如果找不到 _m，用舊邏輯排除 via 找 s0x)
    if current_node == "unknown":
        for item in parts:
            item_low = item.strip().lower()
            if "s0" in item_low and "via" not in item_low and not item_low.startswith("l"):
                match = re.search(r'(s\d+)', item_low)
                if match:
                    current_node = match.group(1)
                    break

    # 2. 抓取該節點的所有有效感測數據 (排除動態路由特徵與收件人)
    for i in range(len(parts)):
        item = parts[i].strip().lower()
        
        # 跳過中繼標籤、發件人標籤與路由層級標籤 (例如 via, _m, L2, L3)
        if "via" in item or "_m" in item or re.match(r'^l\d+$', item): 
            continue
        
        # 避免把最前面的「收件人」(如 s01) 當作數據欄位解析
        if re.match(r'^s\d+$', item):
            continue
            
        for sensor in VALID_SENSORS:
            if item.endswith(sensor) and i + 1 < len(parts):
                val = parts[i+1].strip()
                # 驗證是否為數字 (包含負數與小數)
                if re.match(r'^-?\d+(\.\d+)?$', val):
                    batch_data[sensor] = val
                    break 

    # 3. 獨立抓取 RSSI 通訊品質 
    # 支援格式: "RSSI:-71", "rssi: -71", "rssi=-71"
    rssi_match = re.search(r'rssi\s*[:=]?\s*(-?\d+)', raw_str, re.IGNORECASE)
    if rssi_match:
        batch_data['rssi'] = rssi_match.group(1)
    # 如果是用逗號分隔的格式 (如 ..., rssi, -71, ...)
    elif 'rssi' not in batch_data:
        for i in range(len(parts)):
            if parts[i].strip().lower() == 'rssi' and i + 1 < len(parts):
                val = parts[i+1].strip()
                if re.match(r'^-?\d+$', val):
                    batch_data['rssi'] = val
                    break
                    
    return current_node, batch_data

# --- 主程序 ---
try:
    ser = serial.Serial(COM_PORT, BAUD_RATE, timeout=0.1)
    print(f"✅ LoRa 網關已啟動: {COM_PORT}")
except Exception as e:
    print(f"❌ 無法開啟序列埠: {e}")
    exit()

while True:
    if ser.in_waiting > 0:
        try:
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if "數據:" not in line: 
                continue
            
            payload_str = line.split("數據:")[1].strip()
            
            # ✨ 顯示原始數據功能，方便你監控與除錯硬體
            print(f"📥 收到原始數據: {payload_str}") 
            
            node_id, data_package = extract_universal(payload_str)

            if data_package and node_id != "unknown":
                payload = {"node": node_id, "data": data_package}
                try:
                    res = requests.post(RENDER_URL, json=payload, timeout=8)
                    if res.status_code == 200 or res.status_code == 201:
                        print(f"🚀 [傳送成功] {node_id}: {data_package}")
                    else:
                        print(f"⚠️ [狀態異常] 代碼: {res.status_code}")
                except:
                    print(f"📡 伺服器連線中...")
            else:
                print(f"⚠️ 忽略無效或不完整的封包")
        except Exception as e:
            print(f"⚠️ 解析異常: {e}")
    time.sleep(0.01)