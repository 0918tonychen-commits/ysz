from flask import Flask, render_template, request, jsonify
import time

app = Flask(__name__)

all_sensors = {}  
history_data = {} 
last_seen_map = {} 

@app.route('/')
def index():
    return render_template('index.html')

@app.get("/api/all_data")
def get_all_data():
    now = time.time()
    system_status = "offline"
    for ts in last_seen_map.values():
        if now - ts < 20:
            system_status = "online"
            break
    
    return jsonify({
        **all_sensors,
        "status": system_status,
        "history": history_data
    })

@app.route('/update', methods=['POST'])
def update():
    global all_sensors, history_data, last_seen_map
    data = request.json 
    
    if data and "id" in data:
        raw_id = data["id"].lower() 
        val_str = str(data.get("val", "0"))
        all_sensors[raw_id] = {"val": val_str}
        
        node_id = raw_id.split("_")[0]
        data_type = raw_id.split("_")[1] if "_" in raw_id else "t"
        last_seen_map[node_id] = time.time() 

        if node_id not in history_data:
            history_data[node_id] = {"temp": [], "hum": [], "labels": []}

        try:
            num_val = float(val_str.replace("°C", "").replace("%", "").strip())
            current_time = time.strftime("%H:%M:%S")
            node_hist = history_data[node_id]
            
            if data_type == "t":
                node_hist["temp"].append(num_val)
                node_hist["labels"].append(current_time)
            elif data_type == "h":
                node_hist["hum"].append(num_val)

            # --- 核心修改：限制長度為 100 筆 ---
            if len(node_hist["temp"]) > 100:
                node_hist["temp"].pop(0)
                node_hist["labels"].pop(0)
            if len(node_hist["hum"]) > 100:
                node_hist["hum"].pop(0)
                
        except Exception as e:
            print(f"Data conversion error: {e}")

        return {"status": "success"}, 200
    return {"status": "error"}, 400

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
