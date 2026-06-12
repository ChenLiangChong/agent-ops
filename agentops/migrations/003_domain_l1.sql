-- 003 L1 domain（倉庫數位分身 pipeline）。其他公司流程的 domain 表用後續 migration 各自加。
CREATE TABLE source_documents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER REFERENCES tasks(id),
  doc_type TEXT NOT NULL,              -- 'dwg','dxf','bim','bom','wms_export','plc_tags'
  uri TEXT NOT NULL, sha256 TEXT
);

CREATE TABLE ingest_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER REFERENCES tasks(id),
  status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','ok','error')),
  ir_uri TEXT, error TEXT
);

CREATE TABLE scene_objects (           -- 正規化 IR：設備實例
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ingest_job_id INTEGER REFERENCES ingest_jobs(id),
  canonical_class TEXT NOT NULL,
  vendor_model TEXT,
  pose_x REAL, pose_y REAL, pose_theta REAL,
  footprint_w REAL, footprint_d REAL, height REAL,
  attrs TEXT                           -- JSON
);

CREATE TABLE assets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  usd_url TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('library','generated')),
  semantic_class TEXT,
  bbox_x REAL, bbox_y REAL, bbox_z REAL,
  has_collider INTEGER DEFAULT 0, has_mass INTEGER DEFAULT 0,
  has_physical_material INTEGER DEFAULT 0, has_semantics INTEGER DEFAULT 0,
  provenance TEXT
);

CREATE TABLE asset_matches (
  scene_object_id INTEGER NOT NULL REFERENCES scene_objects(id),
  asset_id INTEGER NOT NULL REFERENCES assets(id),
  score REAL NOT NULL,
  method TEXT NOT NULL CHECK (method IN ('text','image','hybrid')),
  fit_check TEXT CHECK (fit_check IN ('pass','fail')),
  PRIMARY KEY (scene_object_id, asset_id)
);

CREATE TABLE asset_validations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_id INTEGER NOT NULL REFERENCES assets(id),
  rule_id TEXT NOT NULL,               -- SimReady Foundation 規則編號
  status TEXT NOT NULL CHECK (status IN ('pass','fail')),
  detail TEXT
);

CREATE TABLE scenes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER REFERENCES tasks(id),
  usd_stage_uri TEXT NOT NULL,
  site TEXT, layout_version TEXT,
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','validated','published'))
);

CREATE TABLE scene_bindings (
  scene_id INTEGER NOT NULL REFERENCES scenes(id),
  binding_type TEXT NOT NULL CHECK (binding_type IN ('plc','ros','wms','script')),
  endpoint TEXT NOT NULL,
  PRIMARY KEY (scene_id, binding_type, endpoint)
);

CREATE TABLE datasets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scene_id INTEGER NOT NULL REFERENCES scenes(id),
  randomization_config TEXT NOT NULL,  -- JSON：pose/材質/光/相機分佈
  writer TEXT NOT NULL,
  format TEXT NOT NULL CHECK (format IN ('KITTI','COCO','DOPE')),
  num_frames INTEGER NOT NULL,
  seed INTEGER,
  output_uri TEXT
);

CREATE TABLE dataset_artifacts (
  dataset_id INTEGER NOT NULL REFERENCES datasets(id),
  annotator TEXT NOT NULL,             -- 'rgb','bbox_2d_tight','semantic_seg','depth',...
  uri TEXT NOT NULL, count INTEGER,
  PRIMARY KEY (dataset_id, annotator)
);

CREATE TABLE sim_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scene_id INTEGER NOT NULL REFERENCES scenes(id),
  scenario TEXT NOT NULL,
  bindings TEXT, sensor_config TEXT,
  metrics TEXT, logs_uri TEXT,
  approved_by TEXT
);
