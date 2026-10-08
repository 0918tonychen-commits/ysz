# Arduino firmware

`S05_from_S03/` 是目前指定的 S05 正式原始碼，目標板為 Arduino MKR WAN 1310
（FQBN：`arduino:samd:mkrwan1310`）。主程式與 `lora_cad.h` 必須放在同一個
sketch 目錄再由 Arduino IDE 或 Arduino CLI 編譯。

預設拓撲為：

- 節點：`s05`
- 主中繼：`s04`
- 單向上行目標：`s04`
- 層級：L3
- 上報週期：5 分鐘
- LoRa 頻率：921 MHz

目前正式版沒有 CMD、遠端設定或後端下行，只保留直屬中繼的單跳 ACK，供發送端
以嚴格門檻自動切換主／備中繼。移除完整下行前的版本與還原方式記錄在
`archive/S05_DOWNLINK_VERSION.md`。

S02～S05 的完整燒錄範本放在桌面專題資料夾的
`node_firmware_2_/templates/`；此 repository 只保留正式 S05 與版本還原紀錄。
