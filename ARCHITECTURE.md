# 架構

## 三個平面

```
傳輸平面   Linear / Slack / GitHub / CLI（adapters.py — 先 mock，真接口同形狀換 I/O）
              │  驗簽 → 正規化 → 落 DB → verified-actor → 通知（meta 一律字串）
控制平面   gateway.py：claim-lease 派工（queue.py）＋ 記憶注入（memory.py）
              ＋ 審批（approvals.py）＋ 通知（escalations.py）＋ 遙測（telemetry.py）
執行平面   角色 agent（charters/*.md 是資料）× workflow 階段（workflows/*.json 是資料）
              brain 可換：DeterministicBrain（離線）↔ ClaudeCliBrain（claude -p 訂閱）
```

## 一張單的生命週期

```
intake(verified actor) → tasks(queued) → claim(租約 CAS+TTL) → 逐階段：
  注入記憶（blocks 全進、facts top-K＝相似×新近×重要）→ 記 injected_memory_ids
  → brain 執行 → artifact 落檔（DB 存 uri+sha256）
  → 有 spec：評分 → 不過帶缺口重跑（max_revise）→ 仍不過＝task failed＋通知
  → 有 approval：開單（同 tx 發通知）→ 人決 → 逐欄位綁定消費 → 才跑 COMMIT（不可逆）
→ done/failed 通知 → 人類回饋（review 意見/edit diff/merge）→ feedback_events
→ 蒸餾（mem0 兩段式：抽取→對帳 ADD/UPDATE/supersede）→ memory_facts（附 source_quote）
→ 下一張單的注入 ←──────────────────────────── 飛輪閉環
```

## DB（單一 SQLite WAL，migrations-only）

- `001_core.sql`：agents / specs / tasks（工單佇列）/ runs（OTel 欄位命名）/ tool_calls /
  artifacts / grades / approvals / escalations / interaction_log
- `002_memory.sql`：memory_blocks / memory_facts（CHECK 反捏造）/ memory_history / feedback_events
- `003_domain_l1.sql`：倉庫數位分身 11 張表（scene_objects/assets/asset_matches/
  asset_validations/scenes/datasets/...）——其他流程的 domain 表走後續 migration

大件落檔：transcript → `runs/*.jsonl`、artifact 本體 → git/PR，DB 只存路徑＋hash。

## 安全設計（從 sme-ai-kit 實戰移植）

| 機制 | 實作 |
|---|---|
| verified-actor | 身分=adapter 驗簽結果；驗不過 fail-closed `__unverified__`（identity.py） |
| 審批防掉包 | resume_params 逐欄位、型別敏感比對；bool/int/float 不互通；單次消費；72h 過期；tamper 進稽核（rollback 後獨立 tx 寫） |
| 通知硬接線 | enqueue 與業務寫入同 transaction；收件人 enqueue 時定死；投遞租約 CAS 10min 防重送；實際送出文字進稽核 |
| 反捏造 | `CHECK (source_type != 'explicit' OR source_quote IS NOT NULL)` 在 DB 層 |

## 兩道 review 與記憶接線

PR 是唯一交付通道，後面接兩道**刻意不同**的 AI review（都走訂閱 OAuth）：

```
第一道  caveman-review.yml   on: pull_request      單一 PR diff，冷血一行式
        （vendored caveman skill ＋ append repo 鐵律）→ 行內留言 + 一句總結
第二道  leader-review.yml    on: schedule (每小時掃)  確定性 gate 先挑「有新 commit、當前 HEAD 未審過」的
        open PR；沒有就不叫醒 Claude。有目標才用官方 /code-review 底座 ＋ 讀 memory/review-rules.md
        → 查「重蹈覆轍」＋ 跨 PR 架構；審完留 <!-- leader-review-sha --> 標記避免重審
```

**記憶接線**（飛輪 → CI 的那條）：
```
人在 PR 糾正  →  feedback_events  →  distill（mem0 兩段式）  →  memory_facts（附 source_quote）
                                                                      │ agentops export-review-rules
                                                                      ▼
                                          memory/review-rules.md  ──讀──►  第二道 leader review
```
`export_review_rules()`（memory.py）撈未被取代的 facts、按 category 分組、保留出處，
寫成 git-able 的 markdown。CI checkout 後第二道就讀得到——平台學到的規矩自動變成 review 檢查項。
這是平台獨有、現成 review skill 沒有的差異化。

## mock 與真實的邊界

mock 的只有 I/O 兩端，資料形狀照官方：USD Search 回應欄位（url/score/bbox_dimension）、
SimReady Foundation 規則、Replicator BasicWriter annotator 旗標、Linear AgentSessionEvent。
換真後端：`pip install usd-core`（USD）、Linear webhook（傳輸）、`claude setup-token`（CI 訂閱認證）。
`.github/workflows/claude-review.yml` 已是真的——push 上 GitHub 設好 secret 就會動。
