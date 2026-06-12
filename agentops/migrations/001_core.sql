-- 001 控制平面。schema 紀律：本檔凍結後只增不改，新欄位一律走新 migration。
CREATE TABLE agents (
  id TEXT PRIMARY KEY,
  role TEXT NOT NULL,
  charter_path TEXT NOT NULL,
  model TEXT,
  auth_mode TEXT NOT NULL DEFAULT 'subscription'
    CHECK (auth_mode IN ('subscription','oauth_ci','api_key')),
  active INTEGER NOT NULL DEFAULT 1,
  version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE specs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  path TEXT NOT NULL UNIQUE,
  version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow TEXT NOT NULL,
  external_ref TEXT,
  source TEXT NOT NULL CHECK (source IN ('linear','slack','github','cli')),
  title TEXT NOT NULL,
  payload TEXT,
  spec_id INTEGER REFERENCES specs(id),
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued','claimed','running','blocked','review','done','failed','cancelled')),
  assignee_agent TEXT REFERENCES agents(id),
  requested_by TEXT NOT NULL,          -- verified actor：來自 adapter 簽章驗證，絕不採模型自報
  parent_task_id INTEGER REFERENCES tasks(id),
  claimed_at TEXT,
  claim_ttl_sec INTEGER NOT NULL DEFAULT 600,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  done_at TEXT
);

CREATE TABLE runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL REFERENCES tasks(id),
  agent_id TEXT NOT NULL REFERENCES agents(id),
  stage TEXT,
  parent_run_id INTEGER REFERENCES runs(id),
  operation_name TEXT NOT NULL DEFAULT 'invoke_agent',
  provider_name TEXT, request_model TEXT, response_model TEXT,
  auth_mode TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'running'
    CHECK (status IN ('running','ok','error','killed')),
  outcome TEXT,
  error_type TEXT,
  started_at TEXT NOT NULL DEFAULT (datetime('now')),
  ended_at TEXT, duration_ms INTEGER,
  input_tokens INTEGER, output_tokens INTEGER,
  cache_read_tokens INTEGER, cost_usd REAL,
  git_branch TEXT, git_commit TEXT,
  injected_memory_ids TEXT,            -- JSON array；回饋歸因（記憶飛輪閉環）
  transcript_path TEXT
);

CREATE TABLE tool_calls (
  run_id INTEGER NOT NULL REFERENCES runs(id),
  idx INTEGER NOT NULL,
  tool_name TEXT NOT NULL, tool_call_id TEXT,
  success INTEGER, duration_ms INTEGER,
  payload_hash TEXT,
  PRIMARY KEY (run_id, idx)
);

CREATE TABLE artifacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  type TEXT NOT NULL,
  uri TEXT NOT NULL,
  sha256 TEXT,
  spec_id INTEGER REFERENCES specs(id)
);

CREATE TABLE grades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
  spec_id INTEGER REFERENCES specs(id),
  score REAL NOT NULL, passed INTEGER NOT NULL,
  gaps TEXT,
  graded_by_run INTEGER REFERENCES runs(id)
);

CREATE TABLE approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL,
  requester_run INTEGER REFERENCES runs(id),
  summary TEXT NOT NULL,
  resume_action TEXT NOT NULL,
  resume_params TEXT NOT NULL,         -- JSON；消費時逐欄位 exact-match，防掉包
  status TEXT NOT NULL DEFAULT 'waiting'
    CHECK (status IN ('waiting','approved','rejected','expired')),
  approver TEXT, decided_at TEXT,
  expires_at TEXT NOT NULL,
  consumed_at TEXT,
  consumed_by_run INTEGER REFERENCES runs(id)
);

CREATE TABLE escalations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL, summary TEXT NOT NULL, detail TEXT,
  actor TEXT NOT NULL,                 -- 系統蓋章：enqueue 與業務寫入同一 transaction
  target TEXT NOT NULL,                -- 收件人 enqueue 時定死，投遞器永不重算
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','sent','failed')),
  claimed_at TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0,
  sent_at TEXT
);

CREATE TABLE interaction_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor TEXT NOT NULL, action TEXT NOT NULL,
  target_type TEXT, target_id TEXT, detail TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
