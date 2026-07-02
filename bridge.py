import serial
import requests
import time
import re
import sqlite3
import json

# --- 配置區 ---
COM_PORT = 'COM6' # ⚠️ 請根據你電腦的裝置管理員確認你的 COM Port 號碼
BAUD_RATE = 115200 
RENDER_URL = "https://ysz.onrender.com/update"
LOCAL_DB = "gateway_cache.db"
MAX_CACHE_ROWS = 5000  # 快取上限保護，防止空間撐爆

# 🌟 合法感測器白名單（維持與前端完全對齊）
VALID_SENSORS = ['t', 'h', 'c', 'pm25', 'pm10', 'v', 'p', 'lux', 'r_in', 'mcount', 'snr']

# 🌟 全域變數：儲存每個節點上一次成功收到的 MCOUNT 編號
last_mcount_tracker = {}

def init_local_cache():
    """ 初始化本地快取資料庫 """
    conn = sqlite3.connect(LOCAL_DB, timeout=10)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id TEXT,
            payload TEXT,
            timestamp REAL
        )
    ''')
    conn.commit()
    conn.close()

def save_to_local_cache(node_id, data_payload):
    """ 當雲端斷網時，自主防禦轉存至本地 SQLite 確保數據不遺失 """
    print(f"📦 [網關容錯] 遠端連線異常，封包自主存入本地 SQLite 快取。")
    try:
        conn = sqlite3.connect(LOCAL_DB, timeout=10)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO cache (node_id, payload, timestamp) VALUES (?, ?, ?)",
            (node_id, json.dumps(data_payload), time.time())
        )
        conn.commit()

        # 快取筆數上限保護
        cursor.execute("SELECT COUNT(*) FROM cache")
        total = cursor.fetchone()[0]
        if total > MAX_CACHE_ROWS:
            overflow = total - MAX_CACHE_ROWS
            cursor.execute(
                "DELETE FROM cache WHERE id IN (SELECT id FROM cache ORDER BY id ASC LIMIT ?)",
                (overflow,)
            )
            conn.commit()
            print(f"⚠️ [網關容錯] 快取超過上限，已捨棄最舊 {overflow} 筆資料。")
    except sqlite3.Error as e:
        print(f"❌ [快取失敗] 資料庫錯誤: {e}")
    finally:
        if 'conn' in locals():
            conn.close()

def flush_local_cache():
    """ 邏輯：當網路恢復時，自動以非阻塞/快速的形式續傳歷史數據 """
    try:
        conn = sqlite3.connect(LOCAL_DB, timeout=10)
        cursor = conn.cursor()
        cursor.execute("SELECT id, node_id, payload, timestamp FROM cache ORDER BY id ASC")
        cached_rows = cursor.fetchall()
        
        if cached_rows:
            print(f"🔄 [網關自癒] 連線已恢復！開始補傳歷史數據（剩餘 {len(cached_rows)} 筆）...")
            for row in cached_rows:
                db_id, node_id, payload_str, recorded_at = row
                post_payload = {
                    "node": node_id,
                    "data": json.loads(payload_str),
                    "recorded_at": recorded_at
                }
                try:
                    res = requests.post(RENDER_URL, json=post_payload, timeout=1.0)
                    if res.status_code in [200, 201]:
                        cursor.execute("DELETE FROM cache WHERE id = ?", (db_id,))
                        conn.commit() # 即時提交，防止中斷時整批回滾
                    else:
                        print(f"⚠️ [網關自癒] 後端 API 回應異常代碼 {res.status_code}，終止本次補傳。")
                        break
                except requests.RequestException:
                    print("⚠️ [網關自癒] 續傳中斷，雲端連線再度不穩，暫停本次自癒補傳。")
                    break
    except sqlite3.Error as e:
        print(f"❌ [補傳失敗] 資料庫讀取異常: {e}")
    finally:
        if 'conn' in locals():
            conn.close()

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
                    # 先行嘗試補傳舊快取，以維護時間序列正確性
                    flush_local_cache()
                    
                    # 封裝即時遙測負載與目前時間戳記
                    payload = {
                        "node": node_id, 
                        "data": data_package,
                        "recorded_at": time.time()
                    }
                    try:
                        res = requests.post(RENDER_URL, json=payload, timeout=3.0)
                        if res.status_code in [200, 201]:
                            print(f"🚀 [傳送成功] {node_id}: {data_package}")
                        else:
                            print(f"⚠️ [伺服器異常] 狀態碼: {res.status_code}，切換為本地儲存模式。")
                            save_to_local_cache(node_id, data_package)
                    except requests.RequestException as req_err:
                        print(f"🌐 [傳輸超時/失敗] 伺服器無響應，資料已進行本地防禦暫存: {req_err}")
                        save_to_local_cache(node_id, data_package)
            except Exception as e:
                print(f"⚠️ 解析異常: {e}")
        time.sleep(0.01)
except KeyboardInterrupt:
    print("\n🛑 接收到手動終止指令，正在關閉系統...")
finally:
    if 'ser' in locals() and ser.is_open:
        ser.close()
        print("🔌 序列埠埠資源已安全釋放。")