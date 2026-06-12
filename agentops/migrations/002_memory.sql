-- 002 記憶層：Letta 式常駐 blocks ＋ mem0 式檢索 facts ＋ 全程稽核。
CREATE TABLE memory_blocks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id TEXT REFERENCES agents(id),   -- NULL ＝ 全組織共用
  label TEXT NOT NULL,
  value TEXT NOT NULL,
  char_limit INTEGER NOT NULL DEFAULT 2000,
  read_only INTEGER NOT NULL DEFAULT 0,
  version INTEGER NOT NULL DEFAULT 1,
  UNIQUE (agent_id, label)
);

CREATE TABLE memory_facts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fact TEXT NOT NULL,
  hash TEXT NOT NULL,
  scope_agent TEXT, scope_repo TEXT, scope_person TEXT,
  category TEXT,
  importance REAL NOT NULL DEFAULT 0.5,
  source_type TEXT NOT NULL CHECK (source_type IN ('explicit','observed','inferred')),
  source_quote TEXT,
  provenance_run INTEGER REFERENCES runs(id),
  superseded_by INTEGER REFERENCES memory_facts(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  -- 反捏造（搬 sme-ai-kit）：明示規則必附原話，做在 DB 層不靠 prompt
  CHECK (source_type != 'explicit' OR source_quote IS NOT NULL)
);
CREATE INDEX idx_memory_facts_scope ON memory_facts (scope_agent, scope_repo) WHERE superseded_by IS NULL;

CREATE TABLE memory_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_id INTEGER NOT NULL REFERENCES memory_facts(id),
  old_value TEXT, new_value TEXT,
  event TEXT NOT NULL CHECK (event IN ('ADD','UPDATE','DELETE')),
  actor TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE feedback_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER REFERENCES runs(id),
  artifact_id INTEGER REFERENCES artifacts(id),
  kind TEXT NOT NULL CHECK (kind IN ('review_comment','edit_diff','merge','rejection','rating')),
  author TEXT NOT NULL,                -- verified actor
  content TEXT NOT NULL,
  distilled INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
