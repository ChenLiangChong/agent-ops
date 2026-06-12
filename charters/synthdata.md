---
id: synthdata
role: 合成資料生成
auth_mode: subscription
---
# 合成資料 charter

產 Replicator 設定：domain randomization（pose／材質／光照／相機）＋ writer
（BasicWriter annotator 旗標），輸出 KITTI/COCO/DOPE。

## 鐵律
- randomization 分佈與 seed 必須進 datasets 表（可重現）。
- annotator 組合要對齊下游任務（偵測=bbox、分割=seg）。
## Blocker
- spec 要求的 annotator 產不出來。
