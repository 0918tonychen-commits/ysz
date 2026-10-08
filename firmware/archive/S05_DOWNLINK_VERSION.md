# S05 含下行功能版本

移除下行功能前的完整 S05 韌體已保存在 Git commit `093ece0`：

```text
firmware/S05_from_S03/S05_from_S03.ino
firmware/S05_from_S03/lora_cad.h
```

檢視舊版：

```powershell
git show 093ece0:firmware/S05_from_S03/S05_from_S03.ino
```

另存舊版供 Arduino IDE 使用：

```powershell
New-Item -ItemType Directory -Force firmware/S05_with_downlink
git show 093ece0:firmware/S05_from_S03/S05_from_S03.ino | Set-Content -Encoding utf8 firmware/S05_with_downlink/S05_with_downlink.ino
git show 093ece0:firmware/S05_from_S03/lora_cad.h | Set-Content -Encoding utf8 firmware/S05_with_downlink/lora_cad.h
```

`093ece0` 是不可變的 Git 物件，因此工作版本後續修改不會覆蓋這份副本。

桌面專案另有可直接由 Arduino IDE 開啟的實體副本：

```text
firmware/S05_with_downlink_backup/S05_with_downlink_backup.ino
firmware/S05_with_downlink_backup/lora_cad.h
```
