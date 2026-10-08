# LoRa 系統實機驗收

自動測試能驗證資料格式、佇列、API 與資料庫，但不能證明天線、供電、
射頻鏈路或 Windows COM 埠正常。交付前應在實際場地完成以下測試並保存日期、韌體
Git commit、節點編號及序列監控輸出。

## 測試前紀錄

- Arduino 型號：目前編譯資料顯示為 MKR WAN 1310。
- 每個節點的 `nodeID`、層級、指定中繼與感測器。
- 所有節點的頻率、spreading factor、bandwidth、sync word 與 preamble。
- 實際燒錄的 `.ino` 路徑及 Git commit。
- Gateway 電腦、COM port、baud rate、後端 URL。

S02～S05 的完整燒錄範本位於桌面專題的 `node_firmware_2_/templates/`；repository
內保留正式 S05、Gateway Bridge 與後端。驗收時須記錄每塊板子實際使用的範本。

## 驗收矩陣

| 項目 | 操作 | 通過條件 |
| --- | --- | --- |
| 感測器 | 冷開機並觀察 HM3301、SCD30 | 裝置被偵測，數值合理且無持續讀取錯誤 |
| 直接上行 | 節點直接傳至 Gateway | Gateway 解析 node、mcount、boot ID 與感測值 |
| 中繼上行 | 經指定中繼傳送 | 後端 `meta.via`、hop RSSI/SNR 與 Gateway RSSI/SNR 正確 |
| 重複資料 | 重送相同 `event_id` | API 回報 duplicate，資料庫不增加重複 readings |
| Gateway 斷網 | 中斷網路後繼續收 LoRa | SQLite pending 增加；恢復網路後歸零且資料補上 |
| Gateway 重啟 | pending 尚未送完時重啟 | 重啟後繼續補傳，資料不遺失 |
| 單跳 ACK | 發送端各傳一包 | 直屬中繼回覆相同 boot ID、mcount，Gateway 不收到 ACK |
| 主中繼失效 | 關閉 S02 或 S04 | 每週期嘗試兩次，連續五個失敗週期後才切換備援 |
| 主中繼恢復 | 恢復 S02 或 S04 | 每六個週期探測，連續三次成功後才切回主中繼 |
| 備援失效 | 使用備援時關閉備援中繼 | 連續五個失敗週期後回到主中繼重新嘗試 |
| 長時間 | 連續運作至少 24 小時 | 無序列卡死、無 outbox dead letter、丟包率有紀錄 |
| 供電 | 以預定電池／電源運作 | 睡眠、喚醒與低電壓情境符合需求 |

## 建議執行順序

1. 在沒有 LoRa 的情況下先完成 `pytest -q` 與 `scripts/e2e_smoke.py`。
2. 只接一個節點，確認直接上行、SQLite outbox 與網頁顯示。
3. 加入一個中繼，確認 `via` 與每跳無線品質。
4. 分別中斷 S02、S04，確認五週期切換與三次恢復確認門檻。
5. 連續運作至少 24 小時，確認睡眠、CAD、補傳及資料去重穩定。

## 驗收結果

每次測試應填寫：

```text
日期：
地點：
Git commit：
韌體版本／路徑：
節點拓撲：
通過項目：
失敗項目與 log：
測試人：
```
