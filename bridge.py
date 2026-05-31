import serial
import requests
import time
import re

# --- 配置區 ---
COM_PORT = 'COM6' # ⚠️ 請根據你電腦的裝置管理員確認你的 COM Port 號碼
BAUD_RATE = 115200 
RENDER_URL = "https://ysz.onrender.com/update"

# 🌟 將 loss 改為 mcount，作為合法感測器白名單
VALID_SENSORS = ['t', 'h', 'c', 'pm25', 'pm10', 'v', 'p', 'lux', 'r_in', 'mcount', 'snr']

def extract_universal(raw_str):
    parts = raw_str.split(',')
    batch_data = {} 
    current_node = "unknown"
    mcount = None

    # 1. 識別真實數據源頭與「流水號 (mcount)」
    for item in parts:
        item_low = item.strip().lower()
        if "s0" in item_low and "_m" in item_low:
            match = re.search(r'(s\d+)_m(\d+)', item_low)
            if match:
                current_node = match.group(1)
                mcount = int(match.group(2))
                break
                
    if current_node == "unknown":
        for item in parts:
            item_low = item.strip().lower()
            if "s0" in item_low and "via" not in item_low and not item_low.startswith("l"):
                match = re.search(r'(s\d+)', item_low)
                if match:
                    current_node = match.group(1)
                    break

    # 2. 抓取有效感測數據 (優化過濾條件，防範欄位遺漏)
    for i in range(len(parts)):
        item = parts[i].strip().lower()
        
        # 僅排除純路徑標籤與節點定義字串，避免誤殺內含的感測數據項目
        if "via" in item or re.match(r'^l\d+$', item): 
            continue
            
        if re.match(r'^s\d+$', item):
            continue
            
        for sensor in VALID_SENSORS:
            if item == sensor and i + 1 < len(parts):  # 使用精確精準匹配
                val = parts[i+1].strip()
                if re.match(r'^-?\d+(\.\d+)?$', val):
                    batch_data[sensor] = val
                    break 

    # 3. 抓取最後一跳 RSSI
    rssi_match = re.search(r'rssi\s*[:=]?\s*(-?\d+)', raw_str, re.IGNORECASE)
    if rssi_match:
        batch_data['rssi'] = rssi_match.group(1)
    elif 'rssi' not in batch_data:
        for i in range(len(parts)):
            if parts[i].strip().lower() == 'rssi' and i + 1 < len(parts):
                val = parts[i+1].strip()
                if re.match(r'^-?\d+$', val):
                    batch_data['rssi'] = val
                    break

    # 4. 抓取 SNR
    snr_match = re.search(r'snr\s*[:=]?\s*(-?\d+(\.\d+)?)', raw_str, re.IGNORECASE)
    if snr_match:
        batch_data['snr'] = snr_match.group(1)
    
    # ========================================================
    # 🌟 直接裝編號：流水號直傳雲端，降低網關運算開銷
    # ========================================================
    if mcount is not None:
        batch_data['mcount'] = str(mcount)
    elif 'mcount' not in batch_data:
        batch_data['mcount'] = "0" # 防呆

    return current_node, batch_data

# --- 主程序 ---
try:
    ser = serial.Serial(COM_PORT, BAUD_RATE, timeout=0.1)
    print(f"✅ LoRa 網關已啟動: {COM_PORT}")
except Exception as e:
    print(f"❌ 無法開啟序列埠: {e}")
    exit()

try:
    while True:
        if ser.in_waiting > 0:
            try:
                line = ser.readline().decode('utf-8', errors='ignore').strip()
                if "數據:" not in line: 
                    continue
                
                payload_str = line.split("數據:")[1].strip()
                print(f"📥 原始數據: {payload_str}") 
                
                node_id, data_package = extract_universal(payload_str)

                if data_package and node_id != "unknown":
                    payload = {"node": node_id, "data": data_package}
                    try:
                        # ⚠️ Render 免費版有休眠機制，初次連線可能需較長回應時間，此處超時設為 8 秒
                        res = requests.post(RENDER_URL, json=payload, timeout=8)
                        if res.status_code in [200, 201]:
                            print(f"🚀 [傳送成功] {node_id}: {data_package}")
                        else:
                            print(f"⚠️ [伺服器異常] 狀態碼: {res.status_code}")
                    except requests.RequestException as req_err:
                        print(f"🌐 [網路傳輸失敗] 無法連線至 Render: {req_err}")
            except Exception as e:
                print(f"⚠️ 解析異常: {e}")
        time.sleep(0.01)
except KeyboardInterrupt:
    print("\n🛑 收到終止訊號，正在安全關閉 LoRa 網關...")
finally:
    if 'ser' in locals() and ser.is_open:
        ser.close()
        print("🔌 序列埠已安全關閉。")