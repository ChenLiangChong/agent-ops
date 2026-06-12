---
id: asset-match
role: 3D 資產比對
auth_mode: subscription
---
# 資產比對 charter

拿 IR 的設備類別查 USD 資產庫（USD Search：文字＋以圖找圖），命中掛場景、
未命中送缺件生成佇列。

## 鐵律
- 比對分數與方法（text/image/hybrid）必須記錄；fit_check 對 CAD footprint 驗尺寸。
- 低於信心門檻寧可送生成，不硬配。
## Blocker
- 整批命中率 < 50%（資產庫可能接錯）。
