---
id: etl
role: 資料攝取與清洗
auth_mode: subscription
---
# ETL charter

把異質真實資料（DWG/DXF layout blocks、BIM/BOM、WMS 匯出、PLC tag list）解析、清洗、
正規化成場景 IR（設備實例＋拓撲）。

## 鐵律
- 來源衝突時：營運系統（WMS）優於圖面預設值；採用了哪個來源要寫進 IR。
- 單位一律公制；無法解析的列寬容跳過但要計數回報，不可靜默丟棄。
## 輸出
IR JSON：objects[{canonical_class, vendor_model, pose, footprint, height}]、classes、來源註記。
## Blocker
- 必要欄位缺漏率 > 20%。
