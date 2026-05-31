import requests
import time
import re

# --- 配置區 ---
RENDER_URL = "https://ysz.onrender.com/update"
VALID_SENSORS = ['t', 'h', 'c', 'pm25', 'pm10', 'v', 'p', 'lux', 'r_in', 'loss', 'snr']

# 全域變數：儲存修正後的動態統計追蹤器
node_stats = {}

# ==========================================
# 這是你原本 bridge.py 裡一模一樣的完美演算法
# ==========================================
def extract_universal(raw_str):
    parts = raw_str.split(',')
    batch_data = {} 
    current_node = "unknown"
    mcount = None

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

    snr_match = re.search(r'snr\s*[:=]?\s*(-?\d+(\.\d+)?)', raw_str, re.IGNORECASE)
    if snr_match:
        batch_data['snr'] = snr_match.group(1)
    
    # 🌟 核心防禦演算法
    if current_node != "unknown":
        if mcount is not None:
            if current_node not in node_stats:
                node_stats[current_node] = {'first_m': mcount, 'last_m': mcount, 'received_count': 1, 'last_loss': "0.0"}
                batch_data['loss'] = "0.0"
            else:
                stats = node_stats[current_node]
                if mcount < stats['last_m']:
                    stats['first_m'] = mcount
                    stats['last_m'] = mcount
                    stats['received_count'] = 1
                    stats['last_loss'] = "0.0"
                    batch_data['loss'] = "0.0"
                elif mcount == stats['last_m']:
                    batch_data['loss'] = stats['last_loss']
                else:
                    stats['last_m'] = mcount
                    stats['received_count'] += 1
                    expected_total = stats['last_m'] - stats['first_m'] + 1
                    if expected_total > 0:
                        loss_rate = ((expected_total - stats['received_count']) / expected_total) * 100
                        if loss_rate < 0: loss_rate = 0.0
                        stats['last_loss'] = f"{loss_rate:.1f}"
                        batch_data['loss'] = stats['last_loss']
        else:
            batch_data['loss'] = "0.0"

    return current_node, batch_data

# ==========================================
# 🚀 模擬產生器 (代替實體 Arduino)
# ==========================================
print("🚀 啟動純軟體模擬測試 (VS Code 專用)...")
print("不需要插 Arduino！直接在本地端模擬各種極端通訊狀況。\n")

m_count = 1
while True:
    test_cases = []

    # 1. 正常發送 (s02) -> 預期 LOSS 0.0%
    test_cases.append(f"【收到訊號】數據: s01,L2,s02_M{m_count},t,24.9,h,62.6,c,1091, rssi: -58, snr: 13.00")

    # 2. 測試負數 SNR (s05) -> 預期正確抓到 snr: -0.25
    test_cases.append(f"【收到訊號】數據: s01,L3,s05_M{m_count},t,25.5,h,64.9,c,1027,rssi,-74,snr,10.0, rssi: -56, snr: -0.25")

    # 3. 故意製造 50% 掉包 (s04) -> 只有奇數才發送，預期 LOSS 爬升並穩定在 50.0%
    if m_count % 2 != 0:
        test_cases.append(f"【收到訊號】數據: s01,L2,s04_M{m_count},pm25,8,pm10,12, rssi: -55, snr: 11.00")

    # 4. 故意製造重複封包 (s03) -> 同個流水號發兩次，預期防呆機制啟動，LOSS 維持 0.0%
    test_cases.append(f"【收到訊號】數據: s01,L3,s03_M{m_count},pm25,10,pm10,14,rssi,-59,snr,9.5, rssi: -57, snr: 11.25")
    test_cases.append(f"【收到訊號】數據: s01,L3,s03_M{m_count},pm25,10,pm10,14,rssi,-59,snr,9.5, rssi: -57, snr: 11.25")

    # 依序處理並發送至雲端
    for line in test_cases:
        payload_str = line.split("數據:")[1].strip()
        print(f"📥 模擬接收: {payload_str}")
        node_id, data_package = extract_universal(payload_str)

        if data_package and node_id != "unknown":
            payload = {"node": node_id, "data": data_package}
            try:
                res = requests.post(RENDER_URL, json=payload, timeout=8)
                if res.status_code == 200 or res.status_code == 201:
                    print(f"🚀 [傳送成功] {node_id}: {data_package}")
                else:
                    print(f"⚠️ [狀態異常] 代碼: {res.status_code}")
            except Exception as e:
                print(f"📡 伺服器連線中... ({e})")
        time.sleep(1) # 每包間隔 1 秒，讓前端網頁有時間渲染

    # 5. 模擬硬體斷電重啟 (流水號跑到 15 後強制歸零) -> 預期系統全部重新計算
    m_count += 1
    if m_count > 15:
        print("\n--- ⚡ 觸發極端測試：模擬硬體斷電重啟 (流水號歸零) ---")
        m_count = 1
        time.sleep(3)
    print("-" * 50)
    