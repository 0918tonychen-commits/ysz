import time
import json
import sqlite3
import requests

LOCAL_DB = "gateway_cache.db"
MAX_CACHE_ROWS = 5000   # 快取上限保護，防止空間撐爆
FLUSH_BATCH_LIMIT = 10  # 每次補傳最多處理筆數，避免大量補傳時卡住序列埠讀取
MAX_ROW_RETRIES = 5     # 單筆資料連續遭後端拒絕的上限，超過視為異常封包並捨棄


def init_local_cache():
    """ 初始化本地快取資料庫 """
    conn = sqlite3.connect(LOCAL_DB, timeout=10)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id TEXT,
            payload TEXT,
            timestamp REAL,
            retries INTEGER DEFAULT 0
        )
    ''')
    # 相容舊版資料庫：若是舊檔案沒有 retries 欄位，補上去
    cursor.execute("PRAGMA table_info(cache)")
    existing_cols = [c[1] for c in cursor.fetchall()]
    if "retries" not in existing_cols:
        cursor.execute("ALTER TABLE cache ADD COLUMN retries INTEGER DEFAULT 0")
    conn.commit()
    conn.close()


def save_to_local_cache(node_id, data_payload):
    """ 當雲端斷網時，自主防禦轉存至本地 SQLite 確保數據不遺失 """
    print("📦 [網關容錯] 遠端連線異常，封包自主存入本地 SQLite 快取。")
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


def flush_local_cache(backend_url):
    """ 當網路恢復時，以節流方式分批續傳歷史數據，避免長時間阻塞序列埠讀取 """
    try:
        conn = sqlite3.connect(LOCAL_DB, timeout=10)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, node_id, payload, timestamp, retries FROM cache ORDER BY id ASC LIMIT ?",
            (FLUSH_BATCH_LIMIT,)
        )
        cached_rows = cursor.fetchall()

        if cached_rows:
            cursor.execute("SELECT COUNT(*) FROM cache")
            remaining = cursor.fetchone()[0]
            print(f"🔄 [網關自癒] 連線已恢復！本輪補傳 {len(cached_rows)} 筆（佇列總計剩餘 {remaining} 筆）...")
            for row in cached_rows:
                db_id, node_id, payload_str, recorded_at, retries = row
                post_payload = {
                    "node": node_id,
                    "data": json.loads(payload_str),
                    "recorded_at": recorded_at,  # 沿用原始發生時間，避免補傳時序錯亂
                }
                try:
                    res = requests.post(backend_url, json=post_payload, timeout=1.0)
                    if res.status_code in (200, 201):
                        cursor.execute("DELETE FROM cache WHERE id = ?", (db_id,))
                        conn.commit()  # 即時提交，防止中斷時整批回滾
                    else:
                        retries += 1
                        if retries >= MAX_ROW_RETRIES:
                            print(f"❌ [網關自癒] 第 {db_id} 筆資料連續 {retries} 次遭後端拒絕（狀態碼 {res.status_code}），判定為異常封包並捨棄，不再阻擋後續補傳。")
                            cursor.execute("DELETE FROM cache WHERE id = ?", (db_id,))
                        else:
                            print(f"⚠️ [網關自癒] 第 {db_id} 筆資料遭後端拒絕（狀態碼 {res.status_code}），重試次數 {retries}/{MAX_ROW_RETRIES}，跳過並繼續處理下一筆。")
                            cursor.execute("UPDATE cache SET retries = ? WHERE id = ?", (retries, db_id))
                        conn.commit()
                        # 非網路問題（伺服器有回應但拒絕），不中斷本輪補傳，繼續嘗試佇列中其他資料
                except requests.RequestException:
                    print("⚠️ [網關自癒] 續傳中斷，雲端連線再度不穩，暫停本次自癒補傳。")
                    break
    except sqlite3.Error as e:
        print(f"❌ [補傳失敗] 資料庫讀取異常: {e}")
    finally:
        if 'conn' in locals():
            conn.close()


def upload_telemetry(backend_url, node_id, data_payload):
    """ 主體上傳邏輯：先節流補傳舊快取，再送出當前資料；任何失敗都自動轉存本地 """
    flush_local_cache(backend_url)

    post_payload = {
        "node": node_id,
        "data": data_payload,
        "recorded_at": time.time(),
    }
    try:
        res = requests.post(backend_url, json=post_payload, timeout=3.0)
        if res.status_code in (200, 201):
            print(f"🚀 [傳送成功] {node_id}: {data_payload}")
        else:
            print(f"⚠️ [伺服器異常] 狀態碼: {res.status_code}，切換為本地儲存模式。")
            save_to_local_cache(node_id, data_payload)
    except requests.RequestException as req_err:
        print(f"🌐 [傳輸超時/失敗] 伺服器無響應，資料已進行本地防禦暫存: {req_err}")
        save_to_local_cache(node_id, data_payload)
