CREATE TABLE IF NOT EXISTS business_tasks (
    id TEXT PRIMARY KEY,
    session TEXT NOT NULL,
    source_task TEXT,
    goal TEXT NOT NULL,
    constraints TEXT NOT NULL,
    criteria TEXT NOT NULL,
    memory_version INTEGER NOT NULL DEFAULT 2 CHECK(memory_version=2),
    revision INTEGER NOT NULL DEFAULT 0,
    execution_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    progress TEXT NOT NULL DEFAULT '{"milestones":[],"deliverables":[],"blockers":[],"assessment_heads":{}}',
    summary TEXT NOT NULL DEFAULT '',
    summary_input_hash TEXT,
    search_input_hash TEXT,
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS task_session ON business_tasks(session);
CREATE TABLE IF NOT EXISTS task_plans (
    run TEXT PRIMARY KEY,
    plan TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS task_steps (
    run TEXT NOT NULL,
    key TEXT NOT NULL,
    task TEXT NOT NULL,
    source_message_id TEXT,
    base_revision INTEGER NOT NULL,
    applied_revision INTEGER NOT NULL,
    baseline_requirements TEXT NOT NULL,
    task_patch TEXT NOT NULL,
    effective_requirements TEXT NOT NULL,
    scope TEXT NOT NULL,
    status TEXT NOT NULL,
    result TEXT NOT NULL DEFAULT '',
    result_json TEXT,
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(run,key)
);
CREATE TABLE IF NOT EXISTS context_snapshots (
    run TEXT,
    key TEXT,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(run,key)
);
CREATE TABLE IF NOT EXISTS task_criterion_assessments (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    run TEXT NOT NULL,
    step_key TEXT NOT NULL,
    criterion_id TEXT NOT NULL,
    criterion_version INTEGER NOT NULL,
    local INTEGER NOT NULL,
    method TEXT NOT NULL,
    evidence_ref TEXT NOT NULL,
    evidence_version TEXT NOT NULL,
    verdict TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    UNIQUE(run,step_key,criterion_id,criterion_version,evidence_ref,evidence_version,verdict)
);
CREATE TABLE IF NOT EXISTS task_review_requests (
    id TEXT PRIMARY KEY,
    session TEXT NOT NULL,
    task_id TEXT NOT NULL,
    run TEXT NOT NULL,
    step_key TEXT NOT NULL,
    assessment_id TEXT UNIQUE NOT NULL,
    question TEXT NOT NULL,
    state TEXT NOT NULL,
    decision_key TEXT,
    feedback TEXT,
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS task_step_waits (
    run TEXT NOT NULL,
    consumer TEXT NOT NULL,
    producer TEXT NOT NULL,
    criterion_id TEXT NOT NULL,
    criterion_version INTEGER NOT NULL,
    artifact_role TEXT NOT NULL,
    evidence_version TEXT,
    assessment_id TEXT,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(run,consumer,producer,criterion_id,artifact_role)
);
CREATE INDEX IF NOT EXISTS task_assessment_versions ON task_criterion_assessments(task_id,criterion_id,criterion_version);
CREATE INDEX IF NOT EXISTS review_inbox ON task_review_requests(session,state);
CREATE INDEX IF NOT EXISTS wait_assessment ON task_step_waits(assessment_id,state);
