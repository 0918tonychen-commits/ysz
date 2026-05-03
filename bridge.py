import serial
import requests
import time
import re

# --- 配置區 ---
COM_PORT = 'COM4' 
BAUD_RATE = 115200 
RENDER_URL = "https://ysz.onrender.com/update"
# 系統支持的所有感測器標籤
VALID_SENSORS = ['t', 'h', 'c', 'pm25', 'pm10', 'v', 'p', 'lux']

def extract_universal(raw_str):
    """
    智慧解析：自動抓取節點 ID 並過濾中繼站資訊
    """
    parts = raw_str.split(',')
    batch_data = {} 
    current_node = "unknown"

    # 1. 識別真實數據源頭 (排除包含 via 的標籤)
    for item in parts:
        item_low = item.strip().lower()
        if "s0" in item_low and "via" not in item_low:
            match = re.search(r'(s\d+)', item_low)
            if match:
                current_node = match.group(1)
                break

    # 2. 抓取該節點的所有有效感測數據
    for i in range(len(parts)):
        item = parts[i].strip().lower()
        if "via" in item: continue
            
        for sensor in VALID_SENSORS:
            if item.endswith(sensor) and i + 1 < len(parts):
                val = parts[i+1].strip()
                # 驗證是否為數字 (包含負數與小數)
                if re.match(r'^-?\d+(\.\d+)?$', val):
                    batch_data[sensor] = val
                    break 
                    
    return current_node, batch_data

# --- 主程序 ---
try:
    ser = serial.Serial(COM_PORT, BAUD_RATE, timeout=0.1)
    print(f"✅ LoRa 網關已啟動: {COM_PORT}")
except Exception as e:
    print(f"❌ 無法開啟序列埠: {e}"); exit()

while True:
    if ser.in_waiting > 0:
        try:
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if "數據:" not in line: continue
            
            payload_str = line.split("數據:")[1].strip()
            node_id, data_package = extract_universal(payload_str)

            if data_package and node_id != "unknown":
                payload = {"node": node_id, "data": data_package}
                try:
                    res = requests.post(RENDER_URL, json=payload, timeout=8)
                    if res.status_code == 200:
                        print(f"🚀 [傳送成功] {node_id}: {data_package}")
                except:
                    print(f"📡 伺服器喚醒中...")
        except Exception as e:
            print(f"⚠️ 解析異常: {e}")
    time.sleep(0.01)