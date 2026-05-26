import serial
import requests
import time
import re

# --- 配置區 ---
COM_PORT = 'COM6' # ⚠️ 請根據你電腦的裝置管理員確認你的 COM Port 號碼
BAUD_RATE = 115200 
RENDER_URL = "https://ysz.onrender.com/update"

# 🌟 合法感測器白名單（包含中繼強度 r_in、流失率 loss、訊雜比 snr）
VALID_SENSORS = ['t', 'h', 'c', 'pm25', 'pm10', 'v', 'p', 'lux', 'r_in', 'loss', 'snr']

# 🌟 全域變數：儲存修正後的動態統計追蹤器
node_stats = {}

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

    # 2. 抓取有效感測數據
    for i in range(len(parts)):
        item = parts[i].strip().lower()
        
        if "via" in item or "_m" in item or re.match(r'^l\d+$', item): 
            continue
        
        if re.match(r'^s\d+$', item):
            continue
            
        for sensor in VALID_SENSORS:
            if item.endswith(sensor) and i + 1 < len(parts):
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

    # 🌟 4. 抓取 SNR (支援小數點與負數)
    snr_match = re.search(r'snr\s*[:=]?\s*(-?\d+(\.\d+)?)', raw_str, re.IGNORECASE)
    if snr_match:
        batch_data['snr'] = snr_match.group(1)
    
    # ========================================================
    # 🌟 5. 核心演算法修復：完美動態區間封包流失率演算法
    # ========================================================
    if current_node != "unknown" and mcount is not None:
        if current_node not in node_stats:
            # 第一次成功接收該節點的封包：記錄起點流水號，實際收到數初始化為 1
            node_stats[current_node] = {
                'first_m': mcount, 
                'last_m': mcount, 
                'received_count': 1
            }
            batch_data['loss'] = "0.0"
        else:
            stats = node_stats[current_node]
            
            if mcount < stats['last_m']:
                # 異常防禦機制：流水號變小，代表 Arduino 被重開機了，統計追蹤器直接歸零重新出發！
                stats['first_m'] = mcount
                stats['last_m'] = mcount
                stats['received_count'] = 1
                batch_data['loss'] = "0.0"
            elif mcount == stats['last_m']:
                # 重複封包，忽略不計
                pass
            else:
                # 正常遞增或跳號：更新最新流水號，實際收到總數 +1
                stats['last_m'] = mcount
                stats['received_count'] += 1
                
                # 計算這段期間「理論上應該收到」的總包數
                expected_total = stats['last_m'] - stats['first_m'] + 1
                
                if expected_total > 0:
                    loss_rate = ((expected_total - stats['received_count']) / expected_total) * 100
                    if loss_rate < 0: loss_rate = 0.0 # 防呆機制
                    batch_data['loss'] = f"{loss_rate:.1f}"

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
            print(f"📥 原始數據: {payload_str}") 
            
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
        except Exception as e:
            print(f"⚠️ 解析異常: {e}")
    time.sleep(0.01)