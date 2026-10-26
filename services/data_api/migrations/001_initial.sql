CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    session_id TEXT,
    source TEXT NOT NULL,
    channel TEXT NOT NULL,
    direction TEXT NOT NULL,
    event_type TEXT NOT NULL,
    node_id TEXT,
    request_key TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    body_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_run_id ON events(run_id, id);
CREATE INDEX IF NOT EXISTS events_exchange ON events(run_id, session_id, request_key, id);
CREATE INDEX IF NOT EXISTS events_type ON events(event_type, id);
CREATE INDEX IF NOT EXISTS events_source ON events(source, id);
CREATE TABLE IF NOT EXISTS node_snapshots (
    event_row_id INTEGER NOT NULL REFERENCES events(id),
    node_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (event_row_id, node_id)
);
CREATE INDEX IF NOT EXISTS snapshots_node ON node_snapshots(node_id, event_row_id);
CREATE TABLE IF NOT EXISTS control_checks (
    event_row_id INTEGER PRIMARY KEY REFERENCES events(id),
    actor TEXT NOT NULL,
    accepted INTEGER NOT NULL CHECK (accepted IN (0, 1)),
    result_json TEXT NOT NULL,
    upstream_status TEXT NOT NULL CHECK (upstream_status IN ('accepted','rejected','pending'))
);
CREATE TABLE IF NOT EXISTS control_receipts (
    event_row_id INTEGER PRIMARY KEY REFERENCES events(id),
    success INTEGER CHECK (success IN (0, 1)),
    node_id TEXT,
    action TEXT
);
CREATE TABLE IF NOT EXISTS http_exchanges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    status INTEGER NOT NULL,
    body_json TEXT NOT NULL
);
-- Immutable journal, including projections. Updates require a new event.
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS snapshots_no_update BEFORE UPDATE ON node_snapshots
BEGIN SELECT RAISE(ABORT, 'snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS snapshots_no_delete BEFORE DELETE ON node_snapshots
BEGIN SELECT RAISE(ABORT, 'snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS checks_no_update BEFORE UPDATE ON control_checks
BEGIN SELECT RAISE(ABORT, 'checks are immutable'); END;
CREATE TRIGGER IF NOT EXISTS checks_no_delete BEFORE DELETE ON control_checks
BEGIN SELECT RAISE(ABORT, 'checks are immutable'); END;
CREATE TRIGGER IF NOT EXISTS receipts_no_update BEFORE UPDATE ON control_receipts
BEGIN SELECT RAISE(ABORT, 'receipts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS receipts_no_delete BEFORE DELETE ON control_receipts
BEGIN SELECT RAISE(ABORT, 'receipts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS http_no_update BEFORE UPDATE ON http_exchanges
BEGIN SELECT RAISE(ABORT, 'HTTP audit is immutable'); END;
CREATE TRIGGER IF NOT EXISTS http_no_delete BEFORE DELETE ON http_exchanges
BEGIN SELECT RAISE(ABORT, 'HTTP audit is immutable'); END;
