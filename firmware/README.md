# Arduino firmware

`S05_from_S03/` 是目前指定的 S05 正式原始碼，目標板為 Arduino MKR WAN 1310
（FQBN：`arduino:samd:mkrwan1310`）。主程式與 `lora_cad.h` 必須放在同一個
sketch 目錄再由 Arduino IDE 或 Arduino CLI 編譯。

預設拓撲為：

- 節點：`s05`
- 主中繼：`s04`
- 備用中繼：`s02`
- 層級：L3
- 上報週期：5 分鐘
- LoRa 頻率：921 MHz

`.staging/S05_node_firmware/` 是另一份歷史候選版本，不是正式燒錄來源。它會把
備用中繼與切換狀態存入 Flash，資料結構和正式版不同，因此不可在未重新確認
遷移行為前直接替換。

S01–S04 的原始碼目前只在開發者本機，尚未加入此 repository。在這些原始碼補齊
之前，repository 只能完整重建 S05、Gateway 與後端，不能完整重建整個 LoRa 拓撲。
