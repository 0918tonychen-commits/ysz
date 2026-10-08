# 韌體驗證紀錄

## v6：TxDone 等待上限（2026-10-08）

`HOP_ACK_FAILOVER_6` 改用 `LoRa.endPacket(true)` 啟動發送，透過相同 SPI/NSS
輪詢 TxDone，最多等待 5000 ms。開始前清除舊 TxDone；完成或逾時均恢復
Standby 並清除 IRQ。0xFF 的寄存器讀值不視為成功。逾時回報失敗，端點
继续既有重試及備援週期判斷，中繼端回到監聽。資料、ACK、自身資料和轉發
均使用此入口，不再呼叫無限等待的同步 endPacket()。

這個改動限制的是等待 TxDone 的時間，不是底層 SPI 呼叫或 MCU 故障。
尚未在實機注入缺失 TxDone 的故障測試。

四份範本及正式 `S05_from_S03` 皆以 MKR WAN 1310 完整編譯並連結成功
（exit 0）。每份使用 Flash 58,308 bytes、靜態 RAM 5,608 bytes。
已確認所有發送入口只呼叫非同步 `endPacket(true)`，沒有遺留同步呼叫。

## v5 編譯紀錄

日期：2026-10-08。版本：S02～S05 `HOP_ACK_FAILOVER_5`。

四份完整 sketch 皆以 `arduino:samd:mkrwan1310`、Arduino SAMD core 1.8.14
及 arduino-builder 1.6.1 編譯並連結成功（exit 0）。每份使用 Flash 57,796 bytes、
靜態 RAM 5,608 bytes。RAM 數字不包含執行時 String 的 heap 配置。

使用本機 LoRa、Arduino Low Power、RTCZero；缺少的函式庫下載到
`.firmware-validation/libraries`，沒有安裝到使用者的 Arduino libraries：

- [Seeed HM3301](https://github.com/Seeed-Studio/Seeed_PM2_5_sensor_HM3301)，master
- [SparkFun SCD30](https://github.com/sparkfun/SparkFun_SCD30_Arduino_Library)，main
- [FlashStorage](https://github.com/cmaglie/FlashStorage)，master

完整編譯存在第三方函式庫警告，包括 LoRa 過時的二進位常數、Seeed 的 signed/unsigned
比較、RTCZero 的 oldTime 可能未初始化；沒有宣稱無警告。這些函式庫未修改。

本次修正 HM3301 的 29-byte 資料 checksum：前 28 bytes 的 uint8_t 累加需等於
byte 28，才上傳 PM2.5 / PM10。校驗失敗仍傳其他資料與 boot/mcount。
端點每個採樣週期強制 I2C 探測，避免 millis() 在深度睡眠停止時延後熱插拔探測。

尚未進行實機燒錄、連續收包、拔除主中繼的切換及恢復測試。編譯成功不能證明
RF 連線與睡眠喚醒成功。單跳 ACK 只表示中繼確認，不表示 Gateway 或後端已收到。
