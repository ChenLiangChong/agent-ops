# 組織記憶 → review 規矩

> 自動投影，**不要手改**。由 `agentops export-review-rules` 從平台 `memory_facts` 產生，
> 隨記憶飛輪更新。第二道 leader review（`.github/workflows/leader-review.yml`）載入它，
> 查 PR 有沒有重蹈過去被糾正過的問題。每條附出處（source_quote）。

## convention
- **#1** `[etl]` rack 高度請以 WMS 的 rack_height 欄位為準，CAD 圖層預設值不可信　_出處：「rack 高度請以 WMS 的 rack_height 欄位為準，CAD 圖層預設值不可信」_
- **#3** `[scribe]` 週報開頭要先講重點結論，再放數字明細

## gotcha
- **#2** `[engineer]` 修 bug 的 PR 一定要附上測試，沒有測試不收　_出處：「修 bug 的 PR 一定要附上測試，沒有測試不收」_
