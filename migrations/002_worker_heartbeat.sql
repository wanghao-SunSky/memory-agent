-- A worker writes a heartbeat independently of slow model requests.
CREATE TABLE worker_heartbeats (
    worker_id TEXT PRIMARY KEY,
    model_mode TEXT NOT NULL,
    seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
