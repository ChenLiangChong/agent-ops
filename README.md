# agent-ops — 公司代理人平台

**一個全公司共用的代理人身分，接得下公司所有想得到的流程，而且越用越像你的員工。**

不是「一個很強的 chatbot」，是一個治理平面：工單從 Linear / Slack / GitHub / CLI 進來，
閘道驗身分、派工給角色化 sub-agent 跑完整 pipeline（含資料 ETL 前段），交付物一律走 PR，
CI 用**訂閱** OAuth token 自動 review，人類在 merge 點把關；所有人類回饋蒸餾成
**有出處的組織記憶**，下次派工自動注入——教一次，整個平台都學會。

```bash
python3 run_demo.py              # 六幕完整 demo：零依賴、免 API key、離線可跑
python3 tests/test_platform.py  # 8/8
python3 -m agentops init         # 建 DB＋註冊角色
python3 -m agentops dispatch simready-l1 -t "倉庫A 數位分身" -p fixtures/warehouse_a.json
python3 -m agentops work         # 認領執行（互動審批）
```

## demo 演什麼（六幕）

| 幕 | 內容 | 證明的能力 |
|---|---|---|
| 一 | 倉庫 CAD＋WMS → SimReady twin → 合成資料（6 階段） | 完整 pipeline 含 ETL、評分不過自動修訂（iterate→grade→revise） |
| 二 | PM 留 review 意見 → 蒸餾成記憶 | 回饋→記憶管線；出處（source_quote）是 **DB 約束**不是 prompt 客氣話 |
| 三 | 同一張單重派 → 行為改變 | **越用越像員工**：記憶注入＋`runs.injected_memory_ids` 歸因鏈 |
| 四 | 工程流程 fail → 學 → pass | 同一套記憶飛輪直接套到 code-change，平台是通用的 |
| 五 | 審批掉包攻擊被拒 | 核准綁「動作＋參數」逐欄位驗證、單次消費、稽核留痕 |
| 六 | 週報草稿＋人改 diff | 第二條學習訊號（Shopify 驗證過的 edit-diff 機制） |

收尾遙測：全程 **訂閱 21 runs、API 成本 $0**。

## 設計立場

1. **公司流程是資料不是程式**：`workflows/*.json`（階段×角色×spec×審批點）＋ `charters/*.md`（角色鐵律）。加一條流程不改平台。
2. **身分不經過模型**（verified-actor）：requested_by／feedback author 只能來自 adapter 驗簽，模型自報一律不信。
3. **審批（擋路）≠ 通知（不擋路）**：approvals 綁參數單次消費；escalations 與業務寫入同 transaction、笨投遞器＋租約防重送。
4. **過不了 spec 不出貨**：每階段可掛機器可驗的 spec，評分不過帶缺口重跑。
5. **記憶兩層＋全程稽核**：常駐 blocks（角色鐵律）＋檢索 facts（組織經驗），每次變動進 memory_history，每條 explicit fact 必附原話。
6. **模型存取按場景路由**：互動/角色 agent 走訂閱（`claude -p`）；CI review 走訂閱 OAuth token（`.github/workflows/claude-review.yml`）；headless 上量才用 API key。`runs.auth_mode` 全程留痕。

## 兩道 review（縱深防禦，都走訂閱）

PR 交付後有兩道獨立的 AI review，刻意用**不同方式**，不是重複看同樣的東西：

| | 第一道 `caveman-review.yml` | 第二道 `leader-review.yml` |
|---|---|---|
| 觸發 | 每個 PR push | 平日排程 cron ＋可手動 |
| 範圍 | 單一 PR 的 diff | 跨所有 open PR ＋系統性／架構問題 |
| skill | **caveman**（vendored，MIT，71.8k★）冷血一行式 | 官方 **/code-review**（5-agent＋信心分數）當底座 |
| 獨有 | repo 專屬鐵律（審批繞過、SQL 拼接、verified-actor 偽造） | **載入 `memory/review-rules.md`** |
| 認證 | 訂閱 OAuth | 訂閱 OAuth |

第二道的 `memory/review-rules.md` 是平台用 `agentops export-review-rules` 從組織記憶投影出來的——**人在 PR 上糾正一次 → 蒸餾進記憶 → 投影成 review 規矩 → 下次 leader review 自動拿來查「有沒有重蹈覆轍」**。這條線把記憶飛輪接到了 CI，是任何現成 review skill 都沒有的：reviewer 越用越像看過這個 repo 很久的資深 leader。第一道防低級錯誤，第二道防「每個 PR 單看都 OK、合起來架構爛掉」。

## 架構與來歷

設計細節見 [`ARCHITECTURE.md`](ARCHITECTURE.md)。
完整研究與計畫（Shopify 三層架構查證、DDL 全文、分階段路線）見
`../metai/02-agent-ops-完整計畫.md`。

核心機制移植自作者已上線的系統：審批參數綁定／硬接線通知／反捏造記憶（sme-ai-kit，
台灣中小企業 AI 營運中樞）；spec→grade→revise 執行迴圈（simready-copilot）；
MCP 整合肌肉（mcp-taiwan-legal-db 135★ 等 7 個自建 MCP server）。
