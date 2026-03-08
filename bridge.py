import serial
import requests
import time

# --- 1. 設定區 ---
COM_PORT = 'COM3'  
BAUD_RATE = 9600
RENDER_URL = "https://ysz.onrender.com/update"
last_update = {}   

try:
    ser = serial.Serial(COM_PORT, BAUD_RATE, timeout=1)
    print(f"✅ 成功連線至接收端 {COM_PORT}")
except Exception as e:
    print(f"❌ 無法開啟序列埠: {e}")
    exit()

while True:
    if ser.in_waiting > 0:
        try:
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if not line: continue

            print(f"📡 收到原始數據: {line}")

            if "數據:" in line:
                # 這裡會取得 s01_t,22.3,h,78.1,req_time (假設發送端有加 req_time)
                clean_data = line.split("數據:")[1].strip().lower() 
                parts = clean_data.split(",")
                
                if len(parts) >= 4:
                    node_id_base = parts[0].split('_')[0]
                    temp = parts[1]
                    hum = parts[3]

                    # --- 核心修改：處理校時請求 ---
                    if "req_time" in clean_data:
                        # 格式化當前電腦時間，例如 "TIME:14:30:05"
                        current_time_str = time.strftime("TIME:%H:%M:%S")
                        # 透過序列埠傳回給 Arduino 接收端
                        ser.write((current_time_str + "\n").encode()) 
                        print(f"⏰ 已發送校時訊號: {current_time_str}")

                    now = time.time()
                    if node_id_base in last_update and (now - last_update[node_id_base] < 3):
                        continue

                    # 轉發至雲端邏輯維持不變
                    t_payload = {"id": f"{node_id_base}_t", "val": temp}
                    h_payload = {"id": f"{node_id_base}_h", "val": hum}
                    
                    try:
                        r_t = requests.post(RENDER_URL, json=t_payload, timeout=5)
                        r_h = requests.post(RENDER_URL, json=h_payload, timeout=5)
                        
                        if r_t.status_code == 200:
                            print(f"🚀 成功轉發 {node_id_base}：溫 {temp} / 濕 {hum}")
                        else:
                            print(f"❌ 雲端拒絕: {r_t.status_code}")
                    except Exception as req_e:
                        print(f"🌐 網路連線失敗: {req_e}")
                    
                    last_update[node_id_base] = now
            else:
                print("⚠️ 格式不符，跳過解析")

        except Exception as e:
            print(f"⚠️ 解析錯誤: {e}")
            
    time.sleep(0.1)