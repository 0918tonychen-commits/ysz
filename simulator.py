import requests
import time
import random

# 請改為您的 Render 網址
RENDER_URL = "https://ysz.onrender.com/update"

# 模擬五個節點的初始數值
nodes = {
    "s01": {"t": 25.0, "h": 60.0},
    "s02": {"t": 22.0, "h": 55.0},
    "s03": {"t": 28.0, "h": 40.0},
    "s04": {"t": 20.0, "h": 70.0},
    "s05": {"t": 24.0, "h": 50.0}
}

print("🚀 SCADA 虛擬節點模擬器啟動中...")

while True:
    # 隨機挑選一個節點發送數據 (模擬 LoRa 的隨機性)
    node_id = random.choice(list(nodes.keys()))
    
    # 讓數值產生微幅跳動，模擬真實感測器
    nodes[node_id]["t"] += round(random.uniform(-0.5, 0.5), 1)
    nodes[node_id]["h"] += round(random.uniform(-1.0, 1.0), 1)
    
    # 封裝數據 (配合您 main.py 的格式：ID_T 與 ID_H)
    t_payload = {"id": f"{node_id}_t", "val": str(nodes[node_id]["t"])}
    h_payload = {"id": f"{node_id}_h", "val": str(nodes[node_id]["h"])}
    
    try:
        # 發送溫度與濕度
        requests.post(RENDER_URL, json=t_payload, timeout=5)
        requests.post(RENDER_URL, json=h_payload, timeout=5)
        print(f"✅ 已傳送 [{node_id.upper()}] -> 溫度: {nodes[node_id]['t']}°C, 濕度: {nodes[node_id]['h']}%")
    except Exception as e:
        print(f"❌ 傳送失敗: {e}")

    # 模擬發送間隔：隨機休息 2~5 秒 (測試防暴衝與多點更新)
    time.sleep(random.uniform(2, 5))