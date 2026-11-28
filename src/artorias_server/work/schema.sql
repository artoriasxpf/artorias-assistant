CREATE TABLE IF NOT EXISTS work_plans (
 id TEXT PRIMARY KEY, run TEXT NOT NULL, step_key TEXT NOT NULL, task_id TEXT NOT NULL,
 session TEXT NOT NULL, workspace_id TEXT NOT NULL, revision INTEGER NOT NULL,
 status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', calls INTEGER NOT NULL DEFAULT 0,
 call_limit INTEGER NOT NULL, generation INTEGER NOT NULL DEFAULT 0, UNIQUE(run,step_key)
);
CREATE INDEX IF NOT EXISTS work_session ON work_plans(session,status);
CREATE TABLE IF NOT EXISTS work_units (
 id TEXT PRIMARY KEY, plan TEXT NOT NULL, ordinal INTEGER NOT NULL, kind TEXT NOT NULL,
 inputs TEXT NOT NULL, dependencies TEXT NOT NULL, status TEXT NOT NULL,
 summary TEXT NOT NULL DEFAULT '', evidence TEXT, UNIQUE(plan,ordinal)
);
CREATE TABLE IF NOT EXISTS work_attempts (
 id TEXT PRIMARY KEY, unit TEXT NOT NULL, status TEXT NOT NULL, calls INTEGER NOT NULL DEFAULT 0,
 started_at INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE UNIQUE INDEX IF NOT EXISTS work_active_attempt ON work_attempts(unit) WHERE status='running';
CREATE TABLE IF NOT EXISTS work_commands (
 session TEXT NOT NULL, request_key TEXT NOT NULL, plan TEXT NOT NULL, input TEXT NOT NULL,
 PRIMARY KEY(session,request_key)
);
CREATE TABLE IF NOT EXISTS work_task_budgets (
 task_id TEXT PRIMARY KEY, calls INTEGER NOT NULL DEFAULT 0, call_limit INTEGER NOT NULL
);
