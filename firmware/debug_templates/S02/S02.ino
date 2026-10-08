
//  LoRa 上行韌體 - S02 單跳 ACK 嚴格備援版
//  適用板子:SAMD 系列 (Wio、XIAO 等,需有 FlashStorage)
// =================================================================
//  保留感測資料上行、CAD 中繼、睡眠、boot_id 與 mcount。
//  只加入直屬中繼 ACK 供本機備援判斷；無 CMD、遠端設定或後端下行。
// ==============================================================.
#include <SPI.h>
#include <LoRa.h>
#include <Wire.h>
#include "Seeed_HM330X.h"
#include "SparkFun_SCD30_Arduino_Library.h"
#include "ArduinoLowPower.h"
#include <FlashStorage.h>
#include "lora_cad.h"

enum PacketKind {
  PKT_DATA,
  PKT_UNKNOWN
};

// 明確宣告函式，避免 Arduino 自動產生錯誤的 prototype
PacketKind classifyPacket(const String& body);
// =================================================================
// 首次燒錄預設值。單向上行版若要修改，需重新燒錄或更新 Flash 設定。
// =================================================================
#define DEFAULT_IS_ROUTER   true
#define DEFAULT_NODE_ID     "s02"
#define DEFAULT_TARGET      "s01"
#define DEFAULT_BACKUP      "s04"
#define DEFAULT_LEVEL       2
#define DEFAULT_INTERVAL_MS 300000UL  // 發送端：每 5 分鐘送一次

// 頻率規劃(所有節點寫死一致,不會被遠端指令改)
#define FREQ_LISTEN   921.0E6    // 中繼監聽下屬用
#define FREQ_FORWARD  923.0E6    // 自己上傳給長官用(L2 要改成 923)

// 上行使用長前導碼，供 CAD 中繼偵測。
#define PREAMBLE_UPLINK    384
#define PREAMBLE_HOP_ACK    24

// 嚴格備援：每週期嘗試兩次，連續五個週期失敗才切換。
#define HOP_ACK_WAIT_MS             3500UL
#define TX_DONE_TIMEOUT_MS          5000UL
#define TX_ATTEMPTS_PER_CYCLE       2
#define FAILOVER_FAILURE_CYCLES     5
#define FAILBACK_PROBE_EVERY_CYCLES 6
#define FAILBACK_SUCCESS_CYCLES     3

// LoRa NSS 腳
#define LORA_NSS_PIN       LORA_DEFAULT_SS_PIN

// === L2 CAD 省電監聽參數 ===
#define L2_CAD_GAP_MS        20
// SF9/BW125, preamble 384, CR4/5: a 255-byte packet takes about 2.80 s.
#define L2_CAD_RX_MS       3500UL

// === 睡眠下限(硬性)===
// ArduinoLowPower 的 SAMD 實作是「秒」解析度:內部做 millis/1000 去設 RTC 鬧鐘。
// 傳入小於 1000ms 會算出 0 秒 → 鬧鐘設在「現在」→ 永遠不會響 → 睡死。
#define MIN_SLEEP_MS       1000

// 睡眠追蹤
#define SLEEP_TRACE        1


// =================================================================
// Flash 儲存的設定結構
// =================================================================
struct NodeConfig {
  uint32_t magic;
  char     nodeID[8];
  char     targetNode[8];
  uint8_t  myLevel;
  uint8_t  isRouter;
  uint32_t sensorInterval;
};

// 發送端版本使用新的 magic，讓已燒過舊韌體的板子只在第一次開機時
// 重新套用 s05 -> s04 / L3 設定。
// 更换 magic，强制清掉曾经烧入板内的 10 秒测试设定。
#define CONFIG_MAGIC 0xC0FFEE52

FlashStorage(configStore, NodeConfig);
NodeConfig cfg;

LoRaCAD cad;

// =================================================================
// 硬體 & 全域狀態
// =================================================================
HM330X hm330x;
SCD30  airSensor;
uint8_t hm_buf[30];
bool hasHM3301 = false;
bool hasSCD30  = false;

// 感測器熱插拔只處理程式已支援的 HM3301 與 SCD30。
// 定期用 I2C 位址確認在線狀態，不改動 LoRa 或睡眠排程。
#define HM3301_I2C_ADDRESS       0x40
#define SCD30_I2C_ADDRESS        0x61
#define SENSOR_PROBE_INTERVAL_MS 5000UL
unsigned long lastSensorProbeAt = 0;

unsigned long msgCount = 0;
unsigned long previousMillis = 0;

char bootId[9] = "00000000";
bool usingBackup = false;
uint8_t activeRelayFailureCycles = 0;
uint8_t primaryRecoverySuccesses = 0;
uint16_t backupCycles = 0;

// =================================================================
// 🌟 sleepMs — 所有睡眠的唯一入口（修正 Serial 卡住 bug）
// =================================================================
void sleepMs(uint32_t ms) {
  if (ms < MIN_SLEEP_MS) ms = MIN_SLEEP_MS;
#if SLEEP_TRACE
  Serial.print("💤 sleep "); Serial.print(ms); Serial.println(" ms");
#endif
  Serial.flush();          // 確保印完
  Serial.end();            // 🌟 關閉 USB Serial,避免醒來時寫入殭屍緩衝區卡住

  LowPower.sleep(ms);

  Serial.begin(115200);    // 🌟 醒來後重新初始化 Serial
  unsigned long t0 = millis();
  while (!Serial && millis() - t0 < 2000);  // 等 USB 重連,最多 2 秒不阻塞
#if SLEEP_TRACE
  Serial.println("⏰ wake");
#endif
}

// =================================================================
// 電池電壓
// =================================================================
#if defined(ADC_BATTERY)
  #define BATTERY_PIN      ADC_BATTERY
  #define BATTERY_DIVIDER  1.275f
#endif

float readBatteryVolts() {
#if defined(BATTERY_PIN)
  analogReadResolution(10);
  int raw = analogRead(BATTERY_PIN);
  return raw * (3.3f / 1023.0f) * BATTERY_DIVIDER;
#else
  return -1.0f;
#endif
}

void mintBootId() {
  uint32_t seed = micros();
  for (int i = 0; i < 8; i++) {
    seed = (seed * 1664525UL + 1013904223UL) ^ (uint32_t)analogRead(A0);
  }
  snprintf(bootId, sizeof(bootId), "%08lX", (unsigned long)seed);
}

// =================================================================
// 設定讀寫
// =================================================================
void loadConfig() {
  cfg = configStore.read();
  if (cfg.magic != CONFIG_MAGIC) {
    memset(&cfg, 0, sizeof(cfg));
    cfg.magic = CONFIG_MAGIC;
    strncpy(cfg.nodeID,     DEFAULT_NODE_ID, sizeof(cfg.nodeID) - 1);
    strncpy(cfg.targetNode, DEFAULT_TARGET,  sizeof(cfg.targetNode) - 1);
    cfg.myLevel = DEFAULT_LEVEL;
    cfg.isRouter = DEFAULT_IS_ROUTER ? 1 : 0;
    cfg.sensorInterval = DEFAULT_INTERVAL_MS;
    configStore.write(cfg);
    Serial.println("💾 首次燒錄,寫入預設 config");
  }
}

// =================================================================
// 感測器熱插拔管理（HM3301 / SCD30）
// =================================================================
bool i2cDevicePresent(uint8_t address) {
  Wire.beginTransmission(address);
  return Wire.endTransmission() == 0;
}

void serviceSensors(bool force = false) {
  unsigned long now = millis();
  if (!force && (unsigned long)(now - lastSensorProbeAt) < SENSOR_PROBE_INTERVAL_MS) return;
  lastSensorProbeAt = now;

  bool hmPresent = i2cDevicePresent(HM3301_I2C_ADDRESS);
  if (!hmPresent && hasHM3301) {
    hasHM3301 = false;
    Serial.println("🔌 HM3301 已離線");
  } else if (hmPresent && !hasHM3301) {
    if (hm330x.init() == NO_ERROR) {
      hasHM3301 = true;
      Serial.println("✅ HM3301 已接入並重新初始化");
    }
  }

  bool scdPresent = i2cDevicePresent(SCD30_I2C_ADDRESS);
  if (!scdPresent && hasSCD30) {
    hasSCD30 = false;
    Serial.println("🔌 SCD30 已離線");
  } else if (scdPresent && !hasSCD30) {
    if (airSensor.begin()) {
      airSensor.setMeasurementInterval(2);
      hasSCD30 = true;
      Serial.println("✅ SCD30 已接入並重新初始化");
    }
  }
}

// =================================================================
// 感測器打包
// =================================================================
String buildMyPayload() {
  // millis() pauses during endpoint deep sleep; probe on every wake/cycle.
  serviceSensors(!cfg.isRouter);
  msgCount++;
  String payload = String(cfg.targetNode) + ",L" + String(cfg.myLevel)
                 + "," + String(cfg.nodeID) + "_M" + String(msgCount)
                 + ",boot," + String(bootId);

  float volts = readBatteryVolts();
  if (volts > 0) payload += ",v," + String(volts, 2);

  if (hasHM3301) {
    if (hm330x.read_sensor_value(hm_buf, 29) == NO_ERROR) {
      uint8_t checksum = 0;
      for (uint8_t i = 0; i < 28; i++) checksum += hm_buf[i];
      if (checksum == hm_buf[28]) {
        uint16_t pm25 = (uint16_t)hm_buf[12] << 8 | hm_buf[13];
        uint16_t pm10 = (uint16_t)hm_buf[14] << 8 | hm_buf[15];
        payload += ",pm25," + String(pm25) + ",pm10," + String(pm10);
      } else {
        Serial.println("HM3301 checksum 錯誤，本次略過 PM 數值");
      }
    } else {
      hasHM3301 = false;
      lastSensorProbeAt = 0;
      Serial.println("⚠️ HM3301 讀取失敗，等待重新偵測");
    }
  }
  if (hasSCD30 && airSensor.dataAvailable()) {
    payload += ",t," + String(airSensor.getTemperature(), 1)
             + ",h," + String(airSensor.getHumidity(), 1)
             + ",c," + String(airSensor.getCO2());
  }
  return payload;
}

// =================================================================
// 封包解析輔助
// =================================================================
String fieldAt(const String& s, int idx) {
  int start = 0;
  int count = 0;
  while (count < idx) {
    int c = s.indexOf(',', start);
    if (c < 0) return "";
    start = c + 1;
    count++;
  }
  int end = s.indexOf(',', start);
  if (end < 0) return s.substring(start);
  return s.substring(start, end);
}

String retargetPayload(const String& payload, const char* target) {
  int firstComma = payload.indexOf(',');
  if (firstComma < 0) return payload;
  return String(target) + payload.substring(firstComma);
}

String sourceNodeFromPayload(const String& payload) {
  String sourceToken = fieldAt(payload, 2);
  int marker = sourceToken.indexOf("_M");
  return marker > 0 ? sourceToken.substring(0, marker) : "";
}

String sourceCountFromPayload(const String& payload) {
  String sourceToken = fieldAt(payload, 2);
  int marker = sourceToken.indexOf("_M");
  return marker > 0 ? sourceToken.substring(marker + 2) : "";
}

void restoreUplinkRadio() {
  LoRa.idle();
  LoRa.setPreambleLength(PREAMBLE_UPLINK);
}

bool waitForHopAck(const char* relayId, unsigned long expectedCount) {
  LoRa.setFrequency(FREQ_FORWARD);
  LoRa.setPreambleLength(PREAMBLE_HOP_ACK);
  LoRa.receive();
  unsigned long startedAt = millis();
  while (millis() - startedAt < HOP_ACK_WAIT_MS) {
    int packetSize = LoRa.parsePacket();
    if (!packetSize) continue;
    String ack = "";
    while (LoRa.available()) ack += (char)LoRa.read();
    if (fieldAt(ack, 0) == String(cfg.nodeID)
        && fieldAt(ack, 1) == "ACK"
        && fieldAt(ack, 2) == String(relayId)
        && fieldAt(ack, 3) == String(bootId)
        && fieldAt(ack, 4) == String(expectedCount)) {
      restoreUplinkRadio();
      Serial.println("✅ 單跳 ACK:" + ack);
      return true;
    }
  }
  restoreUplinkRadio();
  return false;
}

bool sendPacketBounded(const String& payload) {
  if (!LoRa.beginPacket()) {
    LoRa.idle();
    Serial.println("LoRa 無法開始發送，本次失敗");
    return false;
  }
  LoRa.print(payload);
  cad.clearTxDone();
  LoRa.endPacket(true);  // Start TX without the library's unlimited wait.
  if (cad.waitForTxDone(TX_DONE_TIMEOUT_MS)) return true;

  LoRa.idle();
  LoRa.setFrequency(FREQ_LISTEN);
  LoRa.setPreambleLength(PREAMBLE_UPLINK);
  Serial.println("LoRa 發送逾時（5 秒），已停止發送並恢復待機");
  return false;
}

bool sendWithHopAck(const String& basePayload, const char* relayId) {
  String payload = retargetPayload(basePayload, relayId);
  for (uint8_t attempt = 1; attempt <= TX_ATTEMPTS_PER_CYCLE; attempt++) {
    LoRa.setFrequency(FREQ_FORWARD);
    LoRa.setPreambleLength(PREAMBLE_UPLINK);
    delay(random(50, 200));
    digitalWrite(LED_BUILTIN, HIGH);
    bool txResult = sendPacketBounded(payload);
    digitalWrite(LED_BUILTIN, LOW);
    if (txResult && waitForHopAck(relayId, msgCount)) {
      Serial.println("📤 上行已由 " + String(relayId) + " 接收:" + payload);
      return true;
    }
    Serial.println("⚠️ " + String(relayId) + " 第 " + String(attempt)
                   + " 次未收到 ACK");
    delay(random(350, 800));
  }
  return false;
}

void sendHopAck(const String& incoming) {
  String source = sourceNodeFromPayload(incoming);
  String count = sourceCountFromPayload(incoming);
  String sourceBoot = fieldAt(incoming, 4);
  if (source.length() == 0 || count.length() == 0 || sourceBoot.length() == 0) return;
  String ack = source + ",ACK," + String(cfg.nodeID)
             + "," + sourceBoot + "," + count;
  LoRa.setFrequency(FREQ_LISTEN);
  LoRa.setPreambleLength(PREAMBLE_HOP_ACK);
  delay(random(30, 90));
  bool ackSent = sendPacketBounded(ack);
  LoRa.setPreambleLength(PREAMBLE_UPLINK);
  if (ackSent) Serial.println("↩️ 單跳 ACK:" + ack);
  else Serial.println("單跳 ACK 發送失敗");
}

// =================================================================
// classifyPacket
// =================================================================
PacketKind classifyPacket(const String& body) {
  String marker = fieldAt(body, 1);
  if (marker.startsWith("L")) return PKT_DATA;
  return PKT_UNKNOWN;
}

// =================================================================
// setup
// =================================================================
void setup() {
  Serial.begin(115200);
  delay(2000);
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LOW);
  Wire.begin();
  randomSeed(analogRead(0));
  mintBootId();

  loadConfig();

  Serial.println("\n=====================================");
  Serial.print("🚀 節點啟動: ");   Serial.println(cfg.nodeID);
  Serial.print("🔑 bootId: ");     Serial.println(bootId);
  Serial.println("📦 韌體版本: S02_HOP_ACK_FAILOVER_6");
  Serial.print("👑 直屬長官: ");   Serial.println(cfg.targetNode);
  if (!cfg.isRouter) {
    Serial.print("🛟 備援長官: "); Serial.println(DEFAULT_BACKUP);
  }
  Serial.print("🛠️  工作模式: ");  Serial.println(cfg.isRouter ? "L1/L2 中繼監聽模式" : "L3 末端省電模式");
  Serial.print("📶 層級: L");      Serial.println(cfg.myLevel);
  Serial.println("=====================================");

  if (hm330x.init() == NO_ERROR) { hasHM3301 = true; Serial.println("✅ HM3301 就緒"); }
  if (airSensor.begin() == true) {
    hasSCD30 = true;
    airSensor.setMeasurementInterval(2);
    Serial.println("✅ SCD30 就緒");
  }
  lastSensorProbeAt = millis();

  bool loraUp = false;
  for (int attempt = 0; attempt < 5 && !loraUp; attempt++) {
    loraUp = LoRa.begin(FREQ_LISTEN);
    if (!loraUp) { Serial.println("❌ LoRa begin 失敗,1 秒後重試"); delay(1000); }
  }
  if (!loraUp) {
    Serial.println("❌ LoRa 無法初始化,10 秒後重新開機");
    Serial.flush();
    delay(10000);
    NVIC_SystemReset();
  }
  LoRa.setSpreadingFactor(9);
  LoRa.setSignalBandwidth(125E3);
  LoRa.setSyncWord(0x34);
  LoRa.setPreambleLength(PREAMBLE_UPLINK);
  LoRa.enableCrc();
  LoRa.setTxPower(14);

  // Shared radio register helper: routers use CAD, all nodes poll TxDone.
  // begin() selects the same SPI/NSS as LoRa.h for MKR WAN boards.
  cad.begin(LORA_NSS_PIN);
  if (cfg.isRouter) {
    LoRa.receive();
    Serial.println("🎧 中繼站進入常駐監聽模式...");
  }
}

// 前向宣告
void routerHandlePacket(const String& incoming, int hopRssi, float hopSnr);
void routerSendOwnData();

// =================================================================
// 中繼站主邏輯
// =================================================================
unsigned long l2LastOwnDataAt = 0;

void routerLoop() {
  bool activity = cad.detectOnce();

  if (!activity) {
    // Router must remain awake. sleepMs() also waits for USB Serial after
    // wake-up, creating a multi-second receive blind spot.
    delay(L2_CAD_GAP_MS);
    if ((unsigned long)(millis() - l2LastOwnDataAt) >= cfg.sensorInterval) {
      l2LastOwnDataAt = millis();
      routerSendOwnData();
    }
    return;
  }

  Serial.println("📡 CAD 偵測到活動,開啟接收");
  LoRa.setFrequency(FREQ_LISTEN);
  LoRa.receive();

  unsigned long rxStart = millis();
  bool handled = false;
  while (millis() - rxStart < L2_CAD_RX_MS) {
    int packetSize = LoRa.parsePacket();
    if (packetSize) {
      String incoming = "";
      while (LoRa.available()) incoming += (char)LoRa.read();

      int hopRssi = LoRa.packetRssi();
      float hopSnr = LoRa.packetSnr();

      String target = fieldAt(incoming, 0);
      if (target == String(cfg.nodeID)) {
        routerHandlePacket(incoming, hopRssi, hopSnr);
        handled = true;
        break;
      }
    }
  }
  if (!handled) Serial.println("CAD 接收逾時或非給我的封包，繼續監聽");

  if ((unsigned long)(millis() - l2LastOwnDataAt) >= cfg.sensorInterval) {
    l2LastOwnDataAt = millis();
    routerSendOwnData();
  }
}

// =================================================================
// 中繼站:處理封包
// =================================================================
void routerHandlePacket(const String& incoming, int hopRssi, float hopSnr) {
  PacketKind kind = classifyPacket(incoming);

  if (kind == PKT_DATA) {
    Serial.println("📥 收到下屬資料:" + incoming
                   + " | RSSI:" + String(hopRssi) + " SNR:" + String(hopSnr, 1));

    // ACK 只回直屬下屬，不進入 Gateway、後端或網頁控制面。
    sendHopAck(incoming);

    int firstComma = incoming.indexOf(',');
    String rest = incoming.substring(firstComma + 1);
    String forwarded = String(cfg.targetNode) + "," + rest
                     + ",via," + String(cfg.nodeID)
                     + ",r_in," + String(hopRssi)
                     + ",hop_snr," + String(hopSnr, 1);

    LoRa.setFrequency(FREQ_FORWARD);
    delay(random(50, 200));
    bool forwardedOk = sendPacketBounded(forwarded);
    if (forwardedOk) Serial.println("🚀 已轉發:" + forwarded);
    else Serial.println("中繼轉發失敗，回到監聽");

    LoRa.setFrequency(FREQ_LISTEN);
  }
}

// =================================================================
// 中繼站:發送自己的感測資料
// =================================================================
void routerSendOwnData() {
  Serial.println("\n🌀 讀取自身數據...");
  String myData = buildMyPayload();

  LoRa.setFrequency(FREQ_FORWARD);
  delay(random(50, 200));
  bool ownDataOk = sendPacketBounded(myData);
  if (ownDataOk) Serial.println("🚀 自身數據發送:" + myData);
  else Serial.println("自身數據發送失敗，回到監聽");

  LoRa.setFrequency(FREQ_LISTEN);
}

// =================================================================
// L3 主邏輯
// =================================================================
void l3Loop() {
  Serial.println("\n🌀 L3 讀取感測器中...");
  delay(2000);

  String myData = buildMyPayload();
  bool delivered = false;

  if (!usingBackup) {
    delivered = sendWithHopAck(myData, cfg.targetNode);
    if (delivered) {
      activeRelayFailureCycles = 0;
    } else {
      activeRelayFailureCycles++;
      Serial.println("⚠️ 主中繼失敗週期 " + String(activeRelayFailureCycles)
                     + "/" + String(FAILOVER_FAILURE_CYCLES));
      if (activeRelayFailureCycles >= FAILOVER_FAILURE_CYCLES) {
        usingBackup = true;
        activeRelayFailureCycles = 0;
        primaryRecoverySuccesses = 0;
        backupCycles = 0;
        Serial.println("🔀 嚴格門檻成立，切換備援 " + String(DEFAULT_BACKUP));
        delivered = sendWithHopAck(myData, DEFAULT_BACKUP);
      }
    }
  } else {
    backupCycles++;
    bool probePrimary = (backupCycles % FAILBACK_PROBE_EVERY_CYCLES) == 0;
    if (probePrimary) {
      bool primaryOk = sendWithHopAck(myData, cfg.targetNode);
      delivered = primaryOk;
      if (primaryOk) {
        primaryRecoverySuccesses++;
        Serial.println("🔎 主中繼恢復確認 " + String(primaryRecoverySuccesses)
                       + "/" + String(FAILBACK_SUCCESS_CYCLES));
      } else {
        primaryRecoverySuccesses = 0;
        Serial.println("🔎 主中繼仍未穩定，恢復計數歸零");
      }
      if (primaryRecoverySuccesses >= FAILBACK_SUCCESS_CYCLES) {
        usingBackup = false;
        activeRelayFailureCycles = 0;
        primaryRecoverySuccesses = 0;
        delivered = primaryOk;
        Serial.println("🔁 主中繼連續確認成功，切回 " + String(cfg.targetNode));
      }
    }

    if (usingBackup) {
      bool backupOk = sendWithHopAck(myData, DEFAULT_BACKUP);
      delivered = delivered || backupOk;
      if (backupOk) {
        activeRelayFailureCycles = 0;
      } else {
        activeRelayFailureCycles++;
        Serial.println("⚠️ 備援失敗週期 " + String(activeRelayFailureCycles)
                       + "/" + String(FAILOVER_FAILURE_CYCLES));
        if (activeRelayFailureCycles >= FAILOVER_FAILURE_CYCLES) {
          usingBackup = false;
          activeRelayFailureCycles = 0;
          primaryRecoverySuccesses = 0;
          Serial.println("🔁 備援也失效，回到主中繼重新嘗試");
        }
      }
    }
  }

  if (!delivered) Serial.println("❌ 本週期沒有中繼確認接收");

  // 安全睡眠
  // 五分钟周期加入 ±15 秒抖动，避免节点长期在同一时刻碰撞。
  int32_t jitter = random(-15000, 15000);
  int64_t requested = (int64_t)cfg.sensorInterval + jitter;
  if (requested < MIN_SLEEP_MS) requested = MIN_SLEEP_MS;
  uint32_t finalSleep = (uint32_t)requested;

  Serial.print("⏳ 睡眠 "); Serial.print(finalSleep / 1000.0); Serial.println(" 秒");

  LoRa.sleep();
  sleepMs(finalSleep);
}

// =================================================================
// loop
// =================================================================
void loop() {
  if (cfg.isRouter) routerLoop();
  else              l3Loop();
}
