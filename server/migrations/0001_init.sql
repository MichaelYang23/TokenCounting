-- Contributed task records. `body` is the record exactly as submitted (schema v0.1);
-- the other columns are copies used for filtering and statistics.
CREATE TABLE records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contributor TEXT NOT NULL,          -- keyed hash: gh_… (GitHub-verified) or an_… (anonymous)
  record_id TEXT NOT NULL,
  verified INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('published', 'flagged', 'removed')),
  reasons TEXT NOT NULL DEFAULT '[]',  -- JSON list: why a record is flagged
  received_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  date TEXT NOT NULL,
  primary_model TEXT NOT NULL,
  effort TEXT,
  category TEXT,
  outcome TEXT NOT NULL,
  turns INTEGER NOT NULL,
  cost_usd REAL,                       -- at the price table deployed when last evaluated
  body TEXT NOT NULL,
  UNIQUE (contributor, record_id)
);

CREATE INDEX records_by_status ON records (status, id);

-- Records submitted per key (contributor or hashed IP) per UTC day, for rate limits.
CREATE TABLE quotas (
  key TEXT NOT NULL,
  day TEXT NOT NULL,
  used INTEGER NOT NULL,
  PRIMARY KEY (key, day)
);
