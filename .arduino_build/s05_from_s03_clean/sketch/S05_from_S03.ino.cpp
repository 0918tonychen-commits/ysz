#include <Arduino.h>
#line 1 "C:\\Users\\rain9\\OneDrive\\桌面\\ysz\\.staging\\S05_from_S03\\S05_from_S03.ino"

//  LoRa 動態鏈路韌體 v2 - 支援不斷鏈熱插入新節點
//  適用板子:SAMD 系列 (Wio、XIAO 等,需有 FlashStorage)
// =================================================================
//  變動摘要:
//    1. 路由參數改存 Flash,可遠端改,不必重燒
//    2. 新增下行指令(CMD)封包型態
//    3. L3 送完資料後多開 3 秒 RX 視窗接指令
//    4. 中繼站學習下行路由表(從 via 欄位)
//    5. 指令執行後回 ACK(逆向沿路徑上傳)
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
  PKT_CMD,
  PKT_ACK,
  PKT_UNKNOWN
};

// 明確宣告函式，避免 Arduino 自動產生錯誤的 prototype
PacketKind classifyPacket(const String& body);
// =================================================================
// 🌟 首次燒錄預設值 (之後可用遠端指令覆蓋,不必重燒)
// =================================================================
#define DEFAULT_IS_ROUTER   false
#define DEFAULT_NODE_ID     "s05"
#define DEFAULT_TARGET      "s04"
#define DEFAULT_BACKUP      "s02"
#define DEFAULT_LEVEL       3
#define DEFAULT_INTERVAL_MS 300000UL  // 發送端：每 5 分鐘送一次

// S05 平常走 S04；連續三次探測不到目前中繼，才切換另一條線。
#define RELAY_HEALTH_TIMEOUT_MS 2500UL
#define RELAY_FAIL_LIMIT        3

// 頻率規劃(所有節點寫死一致,不會被遠端指令改)
#define FREQ_LISTEN   921.0E6    // 中繼監聽下屬用
#define FREQ_FORWARD  921.0E6    // 自己上傳給長官用(L2 要改成 923)

// === 前導碼長度:上行長、下行短(把互斥的兩個責任拆開)===
#define PREAMBLE_UPLINK    384
#define PREAMBLE_DOWNLINK  24

// === 下行排程視窗參數(Class-A 式)===
#define DOWNLINK_DELAY_MS  30
#define DOWNLINK_RX_MS     600

// LoRa NSS 腳
#define LORA_NSS_PIN       SS

// 中繼站送完自己資料後,聽多久看有沒有下行指令
#define ROUTER_RX_TAIL_MS  600

// L2 下行送出次數
#define CMD_REPEAT_COUNT   1
#define CMD_REPEAT_GAP_MS  20

// === L2 CAD 省電監聽參數 ===
#define L2_CAD_SLEEP_MS    1000
#define L2_CAD_RX_MS       2200

// === 睡眠下限(硬性)===
// ArduinoLowPower 的 SAMD 實作是「秒」解析度:內部做 millis/1000 去設 RTC 鬧鐘。
// 傳入小於 1000ms 會算出 0 秒 → 鬧鐘設在「現在」→ 永遠不會響 → 睡死。
#define MIN_SLEEP_MS       1000

// 睡眠追蹤
#define SLEEP_TRACE        1

// 指令重複執行過濾
#define CMD_HISTORY_SIZE   8
#define ROUTE_TABLE_SIZE   8
#define PENDING_CMD_SIZE   8

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
// 重新套用 s05 -> s04 / L3 設定；後續重啟仍保留遠端設定。
// 更换 magic，强制清掉曾经烧入板内的 10 秒测试设定。
#define CONFIG_MAGIC 0xC0FFEE57

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

char backupTarget[8] = DEFAULT_BACKUP;
bool usingBackup = false;
uint8_t relayFailStreak = 0;

#line 128 "C:\\Users\\rain9\\OneDrive\\桌面\\ysz\\.staging\\S05_from_S03\\S05_from_S03.ino"
const char * activeTarget();
#line 137 "C:\\Users\\rain9\\OneDrive\\桌面\\ysz\\.staging\\S05_from_S03\\S05_from_S03.ino"
void sleepMs(uint32_t ms);
#line 163 "C:\\Users\\rain9\\OneDrive\\桌面\\ysz\\.staging\\S05_from_S03\\S05_from_S03.ino"
float readBatteryVolts();
#line 173 "C:\\Users\\rain9\\OneDrive\\桌面\\ysz\\.staging\\S05_from_S03\\S05_from_S03.ino"
void mintBootId();
#line 194 "C:\\Users\\rain9\\OneDrive\\桌面\\ysz\\.staging\\S05_from_S03\\S05_from_S03.ino"
void loadConfig();
#line 209 "C:\\Users\\rain9\\OneDrive\\桌面\\ysz\\.staging\\S05_from_S03\\S05_from_S03.ino"
void saveConfig();
#line 214 "C:\\Users\\rain9\\OneDrive\\桌面\\ysz\\.staging\\S05_from_S03\\S05_from_S03.ino"
void printConfig();
#line 229 "C:\\Users\\rain9\\OneDrive\\桌面\\ysz\\.staging\\S05_from_S03\\S05_from_S03.ino"
bool isDuplicateCmd(const String& cmdId);
#line 236 "C:\\Users\\rain9\\OneDrive\\桌面\\ysz\\.staging\\S05_from_S03\\S05_from_S03.ino"
void rememberCmd(const String& cmdId);
#line 244 "C:\\Users\\rain9\\OneDrive\\桌面\\ysz\\.staging\\S05_from_S03\\S05_from_S03.ino"
void learnRoute(const String& dest, const String& viaChild);
#line 275 "C:\\Users\\rain9\\OneDrive\\桌面\\ysz\\.staging\\S05_from_S03\\S05_from_S03.ino"
String lookupNextHop(const String& dest);
#line 286 "C:\\Users\\rain9\\OneDrive\\桌面\\ysz\\.staging\\S05_from_S03\\S05_from_S03.ino"
void enqueueCmd(const String& forChild, const String& finalDest, const String& cmdBody);
#line 302 "C:\\Users\\rain9\\OneDrive\\桌面\\ysz\\.staging\\S05_from_S03\\S05_from_S03.ino"
int findPendingForChild(const String& child);
#line 309 "C:\\Users\\rain9\\OneDrive\\桌面\\ysz\\.staging\\S05_from_S03\\S05_from_S03.ino"
void removePending(int idx);
#line 316 "C:\\Users\\rain9\\OneDrive\\桌面\\ysz\\.staging\\S05_from_S03\\S05_from_S03.ino"
bool i2cDevicePresent(uint8_t address);
#line 353 "C:\\Users\\rain9\\OneDrive\\桌面\\ysz\\.staging\\S05_from_S03\\S05_from_S03.ino"
String buildMyPayload();
#line 385 "C:\\Users\\rain9\\OneDrive\\桌面\\ysz\\.staging\\S05_from_S03\\S05_from_S03.ino"
String fieldAt(const String& s, int idx);
#line 399 "C:\\Users\\rain9\\OneDrive\\桌面\\ysz\\.staging\\S05_from_S03\\S05_from_S03.ino"
String extractSource(const String& s);
#line 406 "C:\\Users\\rain9\\OneDrive\\桌面\\ysz\\.staging\\S05_from_S03\\S05_from_S03.ino"
String extractLastVia(const String& s);
#line 418 "C:\\Users\\rain9\\OneDrive\\桌面\\ysz\\.staging\\S05_from_S03\\S05_from_S03.ino"
String executeCommand(const String& cmd, const String& arg);
#line 462 "C:\\Users\\rain9\\OneDrive\\桌面\\ysz\\.staging\\S05_from_S03\\S05_from_S03.ino"
void sendAck(const String& originNode, const String& cmdId, const String& result);
#line 470 "C:\\Users\\rain9\\OneDrive\\桌面\\ysz\\.staging\\S05_from_S03\\S05_from_S03.ino"
void sendDownlinkCmd(const String& nextHop, const String& finalDest, const String& cmdBody);
#line 494 "C:\\Users\\rain9\\OneDrive\\桌面\\ysz\\.staging\\S05_from_S03\\S05_from_S03.ino"
void handleCommand(const String& body, int rssi, float snr);
#line 529 "C:\\Users\\rain9\\OneDrive\\桌面\\ysz\\.staging\\S05_from_S03\\S05_from_S03.ino"
void purgeByCmdId(const String& cmdId);
#line 538 "C:\\Users\\rain9\\OneDrive\\桌面\\ysz\\.staging\\S05_from_S03\\S05_from_S03.ino"
void handleAckForward(const String& body);
#line 554 "C:\\Users\\rain9\\OneDrive\\桌面\\ysz\\.staging\\S05_from_S03\\S05_from_S03.ino"
void tryDeliverPendingTo(const String& child);
#line 571 "C:\\Users\\rain9\\OneDrive\\桌面\\ysz\\.staging\\S05_from_S03\\S05_from_S03.ino"
void setup();
#line 636 "C:\\Users\\rain9\\OneDrive\\桌面\\ysz\\.staging\\S05_from_S03\\S05_from_S03.ino"
void routerLoop();
#line 777 "C:\\Users\\rain9\\OneDrive\\桌面\\ysz\\.staging\\S05_from_S03\\S05_from_S03.ino"
bool relayResponds(const char* relayId);
#line 813 "C:\\Users\\rain9\\OneDrive\\桌面\\ysz\\.staging\\S05_from_S03\\S05_from_S03.ino"
void checkRelayAndFallback();
#line 833 "C:\\Users\\rain9\\OneDrive\\桌面\\ysz\\.staging\\S05_from_S03\\S05_from_S03.ino"
void l3Loop();
#line 900 "C:\\Users\\rain9\\OneDrive\\桌面\\ysz\\.staging\\S05_from_S03\\S05_from_S03.ino"
void loop();
#line 128 "C:\\Users\\rain9\\OneDrive\\桌面\\ysz\\.staging\\S05_from_S03\\S05_from_S03.ino"
const char* activeTarget() {
  return usingBackup ? backupTarget : cfg.targetNode;
}

char bootId[9] = "00000000";

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

// 指令歷史
String recentCmdIds[CMD_HISTORY_SIZE];
int    cmdHistoryHead = 0;

struct RouteEntry { String dest; String viaChild; unsigned long lastSeen; bool inUse; };
RouteEntry routeTable[ROUTE_TABLE_SIZE];

struct PendingCmd { String forChild; String finalDest; String cmdBody; unsigned long queuedAt; int retries; bool inUse; };
PendingCmd pendingCmds[PENDING_CMD_SIZE];

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

void saveConfig() {
  cfg.magic = CONFIG_MAGIC;
  configStore.write(cfg);
}

void printConfig() {
  Serial.println("---------- 目前 Config ----------");
  Serial.print("  nodeID     : "); Serial.println(cfg.nodeID);
  Serial.print("  targetNode : "); Serial.println(cfg.targetNode);
  Serial.print("  backup     : "); Serial.println(backupTarget);
  Serial.print("  active     : "); Serial.println(activeTarget());
  Serial.print("  level      : "); Serial.println(cfg.myLevel);
  Serial.print("  isRouter   : "); Serial.println(cfg.isRouter ? "true" : "false");
  Serial.print("  interval   : "); Serial.print(cfg.sensorInterval / 1000); Serial.println(" s");
  Serial.println("---------------------------------");
}

// =================================================================
// 指令歷史
// =================================================================
bool isDuplicateCmd(const String& cmdId) {
  for (int i = 0; i < CMD_HISTORY_SIZE; i++) {
    if (recentCmdIds[i] == cmdId) return true;
  }
  return false;
}

void rememberCmd(const String& cmdId) {
  recentCmdIds[cmdHistoryHead] = cmdId;
  cmdHistoryHead = (cmdHistoryHead + 1) % CMD_HISTORY_SIZE;
}

// =================================================================
// 下行路由表
// =================================================================
void learnRoute(const String& dest, const String& viaChild) {
  if (dest.length() == 0 || viaChild.length() == 0) return;
  if (dest == String(cfg.nodeID)) return;
  if (viaChild == String(cfg.nodeID)) return;

  for (int i = 0; i < ROUTE_TABLE_SIZE; i++) {
    if (routeTable[i].inUse && routeTable[i].dest == dest) {
      routeTable[i].viaChild = viaChild;
      routeTable[i].lastSeen = millis();
      return;
    }
  }
  for (int i = 0; i < ROUTE_TABLE_SIZE; i++) {
    if (!routeTable[i].inUse) {
      routeTable[i].dest = dest;
      routeTable[i].viaChild = viaChild;
      routeTable[i].lastSeen = millis();
      routeTable[i].inUse = true;
      Serial.println("🗺️  學到路由:" + dest + " via " + viaChild);
      return;
    }
  }
  int oldest = 0;
  for (int i = 1; i < ROUTE_TABLE_SIZE; i++) {
    if (routeTable[i].lastSeen < routeTable[oldest].lastSeen) oldest = i;
  }
  routeTable[oldest].dest = dest;
  routeTable[oldest].viaChild = viaChild;
  routeTable[oldest].lastSeen = millis();
}

String lookupNextHop(const String& dest) {
  for (int i = 0; i < ROUTE_TABLE_SIZE; i++) {
    if (routeTable[i].inUse && routeTable[i].dest == dest)
      return routeTable[i].viaChild;
  }
  return dest;
}

// =================================================================
// 待送佇列
// =================================================================
void enqueueCmd(const String& forChild, const String& finalDest, const String& cmdBody) {
  for (int i = 0; i < PENDING_CMD_SIZE; i++) {
    if (!pendingCmds[i].inUse) {
      pendingCmds[i].forChild = forChild;
      pendingCmds[i].finalDest = finalDest;
      pendingCmds[i].cmdBody = cmdBody;
      pendingCmds[i].queuedAt = millis();
      pendingCmds[i].retries = 0;
      pendingCmds[i].inUse = true;
      Serial.println("📮 指令入列:給 " + forChild + " 送 → " + finalDest + " | " + cmdBody);
      return;
    }
  }
  Serial.println("⚠️  待送佇列已滿,指令丟棄");
}

int findPendingForChild(const String& child) {
  for (int i = 0; i < PENDING_CMD_SIZE; i++) {
    if (pendingCmds[i].inUse && pendingCmds[i].forChild == child) return i;
  }
  return -1;
}

void removePending(int idx) {
  if (idx >= 0 && idx < PENDING_CMD_SIZE) pendingCmds[idx].inUse = false;
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
  serviceSensors();
  msgCount++;
  String payload = String(activeTarget()) + ",L" + String(cfg.myLevel)
                 + "," + String(cfg.nodeID) + "_M" + String(msgCount)
                 + ",boot," + String(bootId);

  float volts = readBatteryVolts();
  if (volts > 0) payload += ",v," + String(volts, 2);

  if (hasHM3301) {
    if (hm330x.read_sensor_value(hm_buf, 29) == NO_ERROR) {
      uint16_t pm25 = (uint16_t)hm_buf[12] << 8 | hm_buf[13];
      uint16_t pm10 = (uint16_t)hm_buf[14] << 8 | hm_buf[15];
      payload += ",pm25," + String(pm25) + ",pm10," + String(pm10);
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

String extractSource(const String& s) {
  int u = s.indexOf('_');
  if (u < 0) return "";
  int start = s.lastIndexOf(',', u);
  return s.substring(start + 1, u);
}

String extractLastVia(const String& s) {
  int lastViaKey = s.lastIndexOf(",via,");
  if (lastViaKey < 0) return "";
  int valueStart = lastViaKey + 5;
  int valueEnd = s.indexOf(',', valueStart);
  if (valueEnd < 0) return s.substring(valueStart);
  return s.substring(valueStart, valueEnd);
}

// =================================================================
// 指令執行
// =================================================================
String executeCommand(const String& cmd, const String& arg) {
  Serial.println("🛠️  執行指令:" + cmd + " arg=" + arg);

  if (cmd == "PING") { return "PONG"; }
  if (cmd == "SET_TARGET") {
    if (arg.length() == 0 || arg.length() >= sizeof(cfg.targetNode)) return "ERR:BAD_ARG";
    strncpy(cfg.targetNode, arg.c_str(), sizeof(cfg.targetNode) - 1);
    cfg.targetNode[sizeof(cfg.targetNode) - 1] = 0;
    usingBackup = false;
    relayFailStreak = 0;
    saveConfig();
    return "OK";
  }
  if (cmd == "SET_BACKUP") {
    if (arg.length() == 0 || arg.length() >= sizeof(backupTarget)) return "ERR:BAD_ARG";
    strncpy(backupTarget, arg.c_str(), sizeof(backupTarget) - 1);
    backupTarget[sizeof(backupTarget) - 1] = 0;
    return "OK";
  }
  if (cmd == "SET_LEVEL") {
    int lv = arg.toInt();
    if (lv < 1 || lv > 5) return "ERR:BAD_ARG";
    cfg.myLevel = lv;
    saveConfig();
    return "OK";
  }
  if (cmd == "SET_INTERVAL") {
    long v = arg.toInt();
    if (v < 10000) return "ERR:TOO_SHORT";
    if (v > 86400000L) return "ERR:TOO_LONG";
    cfg.sensorInterval = (uint32_t)v;
    saveConfig();
    return "OK";
  }
  if (cmd == "PROMOTE") { cfg.isRouter = 1; saveConfig(); return "OK_REBOOT_NEEDED"; }
  if (cmd == "DEMOTE")  { cfg.isRouter = 0; saveConfig(); return "OK_REBOOT_NEEDED"; }
  if (cmd == "REBOOT")  { return "OK_REBOOT_NEEDED"; }
  if (cmd == "DUMP")    { printConfig(); return "OK"; }
  return "ERR:UNKNOWN_CMD";
}

// =================================================================
// ACK / 下行
// =================================================================
void sendAck(const String& originNode, const String& cmdId, const String& result) {
  String ack = String(activeTarget()) + ",ACK," + originNode + "," + cmdId + "," + result;
  LoRa.setFrequency(FREQ_FORWARD);
  delay(random(30, 120));
  LoRa.beginPacket(); LoRa.print(ack); LoRa.endPacket();
  Serial.println("🅰️  送出 ACK:" + ack);
}

void sendDownlinkCmd(const String& nextHop, const String& finalDest, const String& cmdBody) {
  String pkt = nextHop + ",CMD," + finalDest + "," + cmdBody;
  LoRa.setFrequency(FREQ_LISTEN);
  LoRa.setPreambleLength(PREAMBLE_DOWNLINK);
  delay(random(20, 80));
  LoRa.beginPacket(); LoRa.print(pkt); LoRa.endPacket();
  LoRa.setPreambleLength(PREAMBLE_UPLINK);
  Serial.println("⬇️  下行送出:" + pkt);
}

// =================================================================
// classifyPacket
// =================================================================
PacketKind classifyPacket(const String& body) {
  String marker = fieldAt(body, 1);
  if (marker.startsWith("L")) return PKT_DATA;
  if (marker == "CMD") return PKT_CMD;
  if (marker == "ACK") return PKT_ACK;
  return PKT_UNKNOWN;
}

// =================================================================
// 處理指令
// =================================================================
void handleCommand(const String& body, int rssi, float snr) {
  String finalDest = fieldAt(body, 2);
  String cmdId    = fieldAt(body, 3);
  String cmd      = fieldAt(body, 4);
  String arg      = fieldAt(body, 5);

  Serial.println("📩 收到指令封包 dest=" + finalDest + " cmdId=" + cmdId
                 + " cmd=" + cmd + " arg=" + arg
                 + " | rssi=" + String(rssi) + " snr=" + String(snr, 1));

  if (finalDest == String(cfg.nodeID)) {
    if (isDuplicateCmd(cmdId)) {
      Serial.println("♻️  指令重複,只回 ACK 不重跑");
      sendAck(String(cfg.nodeID), cmdId, "OK_DUP");
      return;
    }
    String result = executeCommand(cmd, arg);
    rememberCmd(cmdId);
    sendAck(String(cfg.nodeID), cmdId, result);
    if (result.indexOf("REBOOT_NEEDED") >= 0) {
      delay(500);
      NVIC_SystemReset();
    }
  } else {
    if (!cfg.isRouter) {
      Serial.println("⚠️  我不是中繼站,無法轉發指令");
      return;
    }
    String nextHop = lookupNextHop(finalDest);
    String cmdBody = cmdId + "," + cmd + (arg.length() ? ("," + arg) : "");
    sendDownlinkCmd(nextHop, finalDest, cmdBody);
    enqueueCmd(nextHop, finalDest, cmdBody);
  }
}

void purgeByCmdId(const String& cmdId) {
  for (int i = 0; i < PENDING_CMD_SIZE; i++) {
    if (pendingCmds[i].inUse && pendingCmds[i].cmdBody.startsWith(cmdId + ",")) {
      pendingCmds[i].inUse = false;
      Serial.println("🗑️  ACK 通過,佇列移除:" + cmdId);
    }
  }
}

void handleAckForward(const String& body) {
  String cmdId = fieldAt(body, 3);
  if (cmdId.length()) purgeByCmdId(cmdId);

  int firstComma = body.indexOf(',');
  String rest = body.substring(firstComma + 1);
  String forwarded = String(activeTarget()) + "," + rest;
  LoRa.setFrequency(FREQ_FORWARD);
  delay(random(30, 120));
  LoRa.beginPacket(); LoRa.print(forwarded); LoRa.endPacket();
  Serial.println("🅰️  轉發 ACK 上行:" + forwarded);
}

// =================================================================
// 中繼站:交付待送指令
// =================================================================
void tryDeliverPendingTo(const String& child) {
  int idx = findPendingForChild(child);
  if (idx < 0) return;
  Serial.println("🎯 抓到 " + child + " 上線,送下行進其排程視窗");

  for (int i = 0; i < CMD_REPEAT_COUNT; i++) {
    sendDownlinkCmd(pendingCmds[idx].forChild,
                    pendingCmds[idx].finalDest,
                    pendingCmds[idx].cmdBody);
    delay(CMD_REPEAT_GAP_MS);
  }
  removePending(idx);
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
  Serial.println("📦 韌體版本: S05_FROM_S03_CLEAN_1");
  Serial.print("👑 直屬長官: ");   Serial.println(activeTarget());
  Serial.print("🛟 備用中繼: ");   Serial.println(backupTarget);
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

  if (cfg.isRouter) {
    // CAD 只屬於中繼端。發送端不要碰 CAD helper，避免在 MKR WAN 1310
    // 上以錯誤的 SPI/NSS 操作不存在的外部 LoRa 模組。
    cad.begin(LORA_NSS_PIN);
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
unsigned long l2ElapsedMs = 0;

void routerLoop() {
  bool activity = cad.detectOnce();

  if (!activity) {
    cad.sleep();
    delay(2);
    sleepMs(L2_CAD_SLEEP_MS);
    LoRa.idle();
    delay(2);
    LoRa.setFrequency(FREQ_LISTEN);
    LoRa.setPreambleLength(PREAMBLE_UPLINK);

    l2ElapsedMs += L2_CAD_SLEEP_MS;
    if (l2ElapsedMs >= cfg.sensorInterval) {
      l2ElapsedMs = 0;
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
  if (!handled) Serial.println("💤 CAD 假警報或非給我的封包,回睡");

  l2ElapsedMs += (millis() - rxStart);
  if (l2ElapsedMs >= cfg.sensorInterval) {
    l2ElapsedMs = 0;
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

    String src = extractSource(incoming);
    String lastVia = extractLastVia(incoming);
    String immediateChild = lastVia.length() ? lastVia : src;
    if (src.length()) learnRoute(src, immediateChild);

    tryDeliverPendingTo(immediateChild);

    int firstComma = incoming.indexOf(',');
    String rest = incoming.substring(firstComma + 1);
    String forwarded = String(activeTarget()) + "," + rest
                     + ",via," + String(cfg.nodeID)
                     + ",r_in," + String(hopRssi)
                     + ",snr," + String(hopSnr, 1);

    LoRa.setFrequency(FREQ_FORWARD);
    delay(random(50, 200));
    LoRa.beginPacket(); LoRa.print(forwarded); LoRa.endPacket();
    Serial.println("🚀 已轉發:" + forwarded);

    unsigned long t0 = millis();
    while (millis() - t0 < ROUTER_RX_TAIL_MS) {
      int ps = LoRa.parsePacket();
      if (ps) {
        String down = "";
        while (LoRa.available()) down += (char)LoRa.read();
        int dRssi = LoRa.packetRssi();
        float dSnr = LoRa.packetSnr();
        if (fieldAt(down, 0) == String(cfg.nodeID)
            && classifyPacket(down) == PKT_CMD) {
          handleCommand(down, dRssi, dSnr);
          break;
        }
      }
    }
    LoRa.setFrequency(FREQ_LISTEN);
  }
  else if (kind == PKT_CMD) {
    handleCommand(incoming, hopRssi, hopSnr);
    LoRa.setFrequency(FREQ_LISTEN);
  }
  else if (kind == PKT_ACK) {
    handleAckForward(incoming);
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
  LoRa.beginPacket(); LoRa.print(myData); LoRa.endPacket();
  Serial.println("🚀 自身數據發送:" + myData);

  unsigned long t0 = millis();
  while (millis() - t0 < ROUTER_RX_TAIL_MS) {
    int ps = LoRa.parsePacket();
    if (ps) {
      String down = "";
      while (LoRa.available()) down += (char)LoRa.read();
      int dRssi = LoRa.packetRssi();
      float dSnr = LoRa.packetSnr();
      if (fieldAt(down, 0) == String(cfg.nodeID)
          && classifyPacket(down) == PKT_CMD) {
        handleCommand(down, dRssi, dSnr);
        break;
      }
    }
  }
  LoRa.setFrequency(FREQ_LISTEN);
}

// =================================================================
// S05 中繼健康檢查：主線 S04，備線 S02
// =================================================================
bool relayResponds(const char* relayId) {
  String nonce = String(random(1000, 9999));
  String probe = String(relayId) + ",PROBE," + String(cfg.nodeID) + "," + nonce;

  LoRa.idle();
  LoRa.setFrequency(FREQ_FORWARD);
  LoRa.setPreambleLength(PREAMBLE_UPLINK);
  delay(random(50, 200));
  LoRa.beginPacket();
  LoRa.print(probe);
  LoRa.endPacket();
  Serial.println("🩺 探測中繼:" + probe);

  LoRa.receive();
  unsigned long startedAt = millis();
  while (millis() - startedAt < RELAY_HEALTH_TIMEOUT_MS) {
    int packetSize = LoRa.parsePacket();
    if (!packetSize) continue;

    String response = "";
    while (LoRa.available()) response += (char)LoRa.read();
    if (fieldAt(response, 0) == String(cfg.nodeID)
        && fieldAt(response, 1) == "PROBE_ACK"
        && fieldAt(response, 2) == String(relayId)
        && fieldAt(response, 3) == nonce) {
      LoRa.idle();
      Serial.println("💚 中繼在線:" + response);
      return true;
    }
  }

  LoRa.idle();
  Serial.println("⚠️ 中繼未回應:" + String(relayId));
  return false;
}

void checkRelayAndFallback() {
  const char* checkedRelay = activeTarget();
  if (relayResponds(checkedRelay)) {
    relayFailStreak = 0;
    return;
  }

  relayFailStreak++;
  Serial.println("⚠️ 中繼探測失敗 " + String(relayFailStreak)
                 + "/" + String(RELAY_FAIL_LIMIT));
  if (relayFailStreak < RELAY_FAIL_LIMIT) return;

  usingBackup = !usingBackup;
  relayFailStreak = 0;
  Serial.println("🔀 已切換中繼為 " + String(activeTarget()));
}

// =================================================================
// L3 主邏輯
// =================================================================
void l3Loop() {
  checkRelayAndFallback();

  Serial.println("\n🌀 L3 讀取感測器中...");
  delay(2000);

  String myData = buildMyPayload();

  LoRa.setFrequency(FREQ_FORWARD);
  delay(random(50, 200));
  digitalWrite(LED_BUILTIN, HIGH);
  int packetStarted = LoRa.beginPacket();
  int txResult = 0;
  if (packetStarted) {
    LoRa.print(myData);
    txResult = LoRa.endPacket();
  }
  digitalWrite(LED_BUILTIN, LOW);

  if (packetStarted && txResult) {
    // endPacket() 為阻塞式；走到這裡代表 SX127x 已回報 TxDone。
    Serial.println("✅ TX_DONE L3 上行:" + myData);
  } else {
    Serial.println("❌ TX_FAIL begin=" + String(packetStarted)
                   + " end=" + String(txResult));
  }

  // 下行排程視窗(Class-A 式)
  bool gotSomething = false;
  delay(DOWNLINK_DELAY_MS);
  LoRa.setFrequency(FREQ_FORWARD);
  LoRa.receive();
  Serial.println("👂 開下行視窗 " + String(DOWNLINK_RX_MS) + " ms...");
  unsigned long t0 = millis();
  while (millis() - t0 < DOWNLINK_RX_MS) {
    int ps = LoRa.parsePacket();
    if (ps) {
      String down = "";
      while (LoRa.available()) down += (char)LoRa.read();
      int dRssi = LoRa.packetRssi();
      float dSnr = LoRa.packetSnr();
      if (fieldAt(down, 0) == String(cfg.nodeID)
          && classifyPacket(down) == PKT_CMD) {
        handleCommand(down, dRssi, dSnr);
        gotSomething = true;
        t0 = millis();
      }
    }
  }
  if (!gotSomething) Serial.println("💤 無指令,回睡");

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

