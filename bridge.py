import serial
import time
import re

from gateway_cache import init_local_cache, upload_telemetry

# --- 配置區 ---
COM_PORT = 'COM6' # ⚠️ 請根據你電腦的裝置管理員確認你的 COM Port 號碼
BAUD_RATE = 115200
RENDER_URL = "https://ysz.onrender.com/update"

# 🌟 合法感測器白名單（維持與前端完全對齊）
VALID_SENSORS = ['t', 'h', 'c', 'pm25', 'pm10', 'v', 'p', 'lux', 'r_in', 'mcount', 'snr']

# 🌟 全域變數：儲存每個節點上一次成功收到的 MCOUNT 編號
last_mcount_tracker = {}

# 🌟 全域變數：每個節點的封包遺失統計（自閘道器本次啟動後累計）
node_loss_stats = {}  # node_id -> {"received": int, "lost": int}

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

    # 🌟 核心過濾機制：如果在最源頭就發現編號重複，直接拒絕解析！
    if current_node != "unknown" and mcount is not None:
        if current_node in last_mcount_tracker and last_mcount_tracker[current_node] == mcount:
            print(f"🛡️ [閘道器防禦] 攔截 {current_node} 重複編號 M{mcount}，丟棄該重複封包。")
            return current_node, {} # 回傳空字典，阻斷後續發送
        else:
            # 🌟 封包遺失率統計：MCOUNT 出現跳號代表中間有封包遺失
            prev_mcount = last_mcount_tracker.get(current_node)
            stats = node_loss_stats.setdefault(current_node, {"received": 0, "lost": 0})
            stats["received"] += 1
            if prev_mcount is not None and mcount > prev_mcount + 1:
                stats["lost"] += (mcount - prev_mcount - 1)
            last_mcount_tracker[current_node] = mcount

    # 2. 抓取有效感測數據 (🛡️ 精確化防禦修復：防範 _m 特徵誤殺數據)
    for i in range(len(parts)):
        item = parts[i].strip().lower()
        
        # 只過濾純路徑與純節點前綴，放行包含感測數據的鍵值對
        if "via" in item or re.match(r'^l\d+$', item): 
            continue
        if re.match(r'^s\d+$', item):
            continue
            
        for sensor in VALID_SENSORS:
            # ✨ 精確對齊：直接匹配感測器 Key
            if item == sensor and i + 1 < len(parts):
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
    
    # 裝填編號直傳
    if mcount is not None:
        batch_data['mcount'] = str(mcount)
    elif 'mcount' not in batch_data:
        batch_data['mcount'] = "0"

    # 裝填封包遺失率（自本次啟動累計，百分比，四捨五入到小數點後 1 位）
    if current_node in node_loss_stats:
        stats = node_loss_stats[current_node]
        total = stats["received"] + stats["lost"]
        loss_pct = round(stats["lost"] / total * 100, 1) if total > 0 else 0.0
        batch_data['loss'] = str(loss_pct)

    return current_node, batch_data

# --- 主程序 ---
init_local_cache()

try:
    ser = serial.Serial(COM_PORT, BAUD_RATE, timeout=0.1)
    print(f"✅ LoRa 閘道器已啟動: {COM_PORT}")
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

                # 🌟 只有在 data_package 內部有感測數據時（未被攔截），才觸發雲端 POST
                if data_package and node_id != "unknown":
                    # 先節流補傳舊快取，再送出目前這筆即時資料；任何失敗都自動轉存本地
                    upload_telemetry(RENDER_URL, node_id, data_package)
            except Exception as e:
                print(f"⚠️ 解析異常: {e}")
        time.sleep(0.01)
except KeyboardInterrupt:
    print("\n🛑 接收到手動終止指令，正在關閉系統...")
finally:
    if 'ser' in locals() and ser.is_open:
        ser.close()
        print("🔌 序列埠埠資源已安全釋放。")