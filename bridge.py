import serial
import requests
import time
import re

# --- 配置區域 ---
COM_PORT = 'COM4' 
BAUD_RATE = 115200 
RENDER_URL = "https://ysz.onrender.com/update"
VALID_SENSORS = ['t', 'h', 'c', 'pm25', 'pm10'] # 關注的感測標籤
last_update = {}

def extract_as_batch(raw_str):
    """
    通用解析邏輯：自動識別數據源頭，避開 via_ 中繼標籤
    """
    parts = raw_str.split(',')
    batch_data = {} 
    current_node = "unknown"

    # 1. 尋找真正的數據源頭 (排除 via_)
    for item in parts:
        item_low = item.strip().lower()
        if "s0" in item_low and "via" not in item_low:
            match = re.search(r'(s\d+)', item_low)
            if match:
                current_node = match.group(1)
                break

    # 2. 抓取該節點的所有有效數據
    for i in range(len(parts)):
        item = parts[i].strip().lower()
        if "via" in item: continue # 略過中繼訊號資訊
            
        for sensor in VALID_SENSORS:
            if item.endswith(sensor) and i + 1 < len(parts):
                val = parts[i+1].strip()
                if re.match(r'^-?\d+(\.\d+)?$', val):
                    batch_data[sensor] = val
                    break 
                    
    return current_node, batch_data

# --- 主程序 ---
try:
    ser = serial.Serial(COM_PORT, BAUD_RATE, timeout=0.1)
    print(f"✅ 雙頻網關連線成功: {COM_PORT}")
except Exception as e:
    print(f"❌ 序列埠開啟失敗: {e}"); exit()

while True:
    if ser.in_waiting > 0:
        try:
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if "數據:" not in line: continue
            
            payload_str = line.split("數據:")[1].strip()
            node_id, data_package = extract_as_batch(payload_str)

            if data_package and node_id != "unknown":
                # 同步打包上傳
                payload = {"node": node_id, "data": data_package, "ts": int(time.time())}
                try:
                    res = requests.post(RENDER_URL, json=payload, timeout=10)
                    if res.status_code == 200:
                        print(f"🚀 [同步上傳] {node_id}: {data_package}")
                except:
                    print(f"❌ 雲端喚醒中或網路逾時...")
        except Exception as e:
            print(f"⚠️ 解析異常: {e}")
    time.sleep(0.01)