#ifndef LORA_CAD_H
#define LORA_CAD_H

#include <Arduino.h>
#include <SPI.h>

// =================================================================
// SX1276 暫存器
// =================================================================
#define CAD_REG_OP_MODE        0x01
#define CAD_REG_IRQ_FLAGS      0x12
#define CAD_REG_VERSION        0x42

// =================================================================
// RegOpMode 位元
// =================================================================
#define CAD_LONG_RANGE_MODE    0x80
#define CAD_MODE_SLEEP         0x00
#define CAD_MODE_STANDBY       0x01
#define CAD_MODE_RX_SINGLE     0x06
#define CAD_MODE_CAD           0x07

// =================================================================
// RegIrqFlags 位元
// =================================================================
#define CAD_IRQ_CAD_DETECTED   0x01
#define CAD_IRQ_CAD_DONE       0x04
#define CAD_IRQ_TX_DONE        0x08


class LoRaCAD {
public:
  // ---------------------------------------------------------------
  // 初始化 CAD helper
  // ss 必須與 LoRa 函式庫使用的 NSS 相同
  // ---------------------------------------------------------------
  void begin(int ss) {
#if defined(ARDUINO_SAMD_MKRWAN1300) || defined(ARDUINO_SAMD_MKRWAN1310)
    (void)ss;
    // MKR WAN routes the Murata radio through SPI1 at 200 kHz and uses the
    // variant's dedicated chip-select pin.  SPI/SS addresses the wrong bus.
    _ss = LORA_IRQ_DUMB;
    _spi = &SPI1;
    const uint32_t spiFrequency = 200000;
#else
    _ss = ss;
    _spi = &SPI;
    const uint32_t spiFrequency = 8000000;
#endif

    pinMode(_ss, OUTPUT);
    digitalWrite(_ss, HIGH);

    _spiSettings = SPISettings(
      spiFrequency,
      MSBFIRST,
      SPI_MODE0
    );

    // 讀取晶片版本，SX1276/77/78/79 通常應為 0x12
    uint8_t version = readReg(CAD_REG_VERSION);

    Serial.print("CAD NSS = ");
    Serial.println(_ss);

    Serial.print("SX127x version = 0x");
    if (version < 0x10) {
      Serial.print("0");
    }
    Serial.println(version, HEX);

    if (version == 0x12) {
      Serial.println("CAD SPI 通訊正常");
    } else if (version == 0x00 || version == 0xFF) {
      Serial.println(
        "CAD SPI 通訊失敗，請檢查 NSS、SPI 或 LoRa 模組"
      );
    } else {
      Serial.println(
        "警告：SX127x 版本值不是預期的 0x12"
      );
    }

    // 確保晶片位於 LoRa Standby
    enterStandby();
  }


  // ---------------------------------------------------------------
  // 執行一次 CAD
  //
  // 回傳：
  //   true  = 偵測到 LoRa 前導碼／活動
  //   false = 沒偵測到，或 CAD 逾時
  // ---------------------------------------------------------------
  // Clear a stale TxDone before starting an asynchronous transmission.
  void clearTxDone() {
    writeReg(CAD_REG_IRQ_FLAGS, CAD_IRQ_TX_DONE);
  }

  // Poll only TxDone; parsePacket() would switch the radio out of TX mode.
  // Both completion and timeout leave the radio in standby with clean IRQs.
  bool waitForTxDone(uint32_t timeoutMs) {
    const unsigned long startedAt = millis();
    while ((unsigned long)(millis() - startedAt) < timeoutMs) {
      const uint8_t flags = readReg(CAD_REG_IRQ_FLAGS);
      if (flags != 0xFF && (flags & CAD_IRQ_TX_DONE)) {
        enterStandby();
        clearIrqFlags();
        return true;
      }
      delay(1);
    }
    enterStandby();
    clearIrqFlags();
    return false;
  }

  bool detectOnce(uint16_t timeoutMs = 50) {
    // 從 Sleep/RX 切到 Standby
    enterStandby();

    // 從 Sleep 喚醒後，等待晶振與模式穩定
    delay(2);

    // 清除所有舊 IRQ flags
    clearIrqFlags();

    // 進入 CAD 模式
    writeReg(
      CAD_REG_OP_MODE,
      CAD_LONG_RANGE_MODE | CAD_MODE_CAD
    );

    unsigned long startedAt = millis();
    uint8_t flags = 0;
    bool done = false;

    while ((unsigned long)(millis() - startedAt) < timeoutMs) {
      flags = readReg(CAD_REG_IRQ_FLAGS);

      if (flags & CAD_IRQ_CAD_DONE) {
        done = true;
        break;
      }

      delayMicroseconds(100);
    }

    bool detected =
      done &&
      ((flags & CAD_IRQ_CAD_DETECTED) != 0);

    // 清除這次 CAD 產生的 IRQ
    clearIrqFlags();

    // 回到 Standby，交給呼叫端決定後續是 RX 或 Sleep
    enterStandby();

    return detected;
  }


  // ---------------------------------------------------------------
  // 在指定時間內反覆執行 CAD
  //
  // windowMs：總掃描時間
  // gapMs：每次 CAD 之間的間隔
  // ---------------------------------------------------------------
  bool scanWindow(
    uint16_t windowMs,
    uint16_t gapMs = 8
  ) {
    unsigned long startedAt = millis();

    while (
      (unsigned long)(millis() - startedAt) < windowMs
    ) {
      if (detectOnce()) {
        return true;
      }

      if (gapMs > 0) {
        delay(gapMs);
      }
    }

    return false;
  }


  // ---------------------------------------------------------------
  // 讓 SX127x 進入 LoRa Sleep
  // ---------------------------------------------------------------
  void sleep() {
    writeReg(
      CAD_REG_OP_MODE,
      CAD_LONG_RANGE_MODE | CAD_MODE_SLEEP
    );

    // 等待 Sleep 模式生效
    delayMicroseconds(200);
  }


  // ---------------------------------------------------------------
  // 測試 SPI 是否能讀到 SX127x
  // ---------------------------------------------------------------
  bool communicationOK() {
    return readReg(CAD_REG_VERSION) == 0x12;
  }


  // ---------------------------------------------------------------
  // 讀取 SX127x 版本
  // ---------------------------------------------------------------
  uint8_t readVersion() {
    return readReg(CAD_REG_VERSION);
  }


private:
  int _ss = -1;

  SPISettings _spiSettings;
  SPIClass* _spi = &SPI;


  // ---------------------------------------------------------------
  // 切換至 LoRa Standby
  // ---------------------------------------------------------------
  void enterStandby() {
    writeReg(
      CAD_REG_OP_MODE,
      CAD_LONG_RANGE_MODE | CAD_MODE_STANDBY
    );
  }


  // ---------------------------------------------------------------
  // 清除所有 IRQ flags
  //
  // SX127x 的 IRQ flags 是 write-1-to-clear
  // ---------------------------------------------------------------
  void clearIrqFlags() {
    writeReg(CAD_REG_IRQ_FLAGS, 0xFF);
  }


  // ---------------------------------------------------------------
  // SPI 傳輸
  // ---------------------------------------------------------------
  uint8_t transfer(
    uint8_t address,
    uint8_t value
  ) {
    _spi->beginTransaction(_spiSettings);

    digitalWrite(_ss, LOW);

    _spi->transfer(address);
    uint8_t response = _spi->transfer(value);

    digitalWrite(_ss, HIGH);

    _spi->endTransaction();

    return response;
  }


  // ---------------------------------------------------------------
  // 讀取 SX127x 暫存器
  // bit7 = 0 代表讀取
  // ---------------------------------------------------------------
  uint8_t readReg(uint8_t address) {
    return transfer(
      address & 0x7F,
      0x00
    );
  }


  // ---------------------------------------------------------------
  // 寫入 SX127x 暫存器
  // bit7 = 1 代表寫入
  // ---------------------------------------------------------------
  void writeReg(
    uint8_t address,
    uint8_t value
  ) {
    transfer(
      address | 0x80,
      value
    );
  }
};

#endif // LORA_CAD_H
