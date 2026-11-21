"""Idempotent migrations. UTC timestamps; legacy dates mean migration time."""
import json

NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"


def ensure_timestamps(connection):
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    for (table,) in tables:
        quoted = '"' + table.replace('"', '""') + '"'
        columns = {row[1] for row in connection.execute(f'PRAGMA table_info({quoted})')}
        for field in ('created_at', 'updated_at'):
            if field not in columns:
                connection.execute(f"ALTER TABLE {quoted} ADD COLUMN {field} TEXT NOT NULL DEFAULT ''")
        connection.execute(f"UPDATE {quoted} SET created_at={NOW} WHERE created_at=''")
        connection.execute(f"UPDATE {quoted} SET updated_at=created_at WHERE updated_at=''")
        # All application and current framework tables have rowid. Internal SQLite tables are excluded.
        connection.execute(f'''CREATE TRIGGER IF NOT EXISTS "{table}_timestamps_insert"
            AFTER INSERT ON {quoted}
            BEGIN UPDATE {quoted} SET created_at=CASE WHEN NEW.created_at='' THEN {NOW} ELSE NEW.created_at END,
                updated_at=CASE WHEN NEW.updated_at='' THEN {NOW} ELSE NEW.updated_at END
                WHERE rowid=NEW.rowid; END''')
        connection.execute(f'''CREATE TRIGGER IF NOT EXISTS "{table}_timestamps_update"
            AFTER UPDATE ON {quoted}
            WHEN NEW.updated_at=OLD.updated_at AND OLD.created_at<>''
            BEGIN UPDATE {quoted} SET created_at=OLD.created_at, updated_at={NOW}
                WHERE rowid=NEW.rowid; END''')


def ensure_task_fields(c):
    task_columns = {row[1] for row in c.execute('PRAGMA table_info(business_tasks)')}
    additions = {
        'business_tasks': {
            'revision': 'INTEGER NOT NULL DEFAULT 0',
            'execution_count': 'INTEGER NOT NULL DEFAULT 0',
            'criterias': "TEXT NOT NULL DEFAULT '{}'",
            'progress': "TEXT NOT NULL DEFAULT '[]'",
        },
        'task_steps': {
            'source_message_id': 'TEXT', 'base_revision': 'INTEGER NOT NULL DEFAULT 0',
            'applied_revision': 'INTEGER NOT NULL DEFAULT 0',
            'task_patch': "TEXT NOT NULL DEFAULT '{}'",
            'effective_requirements': "TEXT NOT NULL DEFAULT '{}'",
            'baseline_requirements': "TEXT NOT NULL DEFAULT '{}'",
        },
    }
    for table, fields in additions.items():
        columns = {row[1] for row in c.execute(f'PRAGMA table_info({table})')}
        for name, spec in fields.items():
            if name not in columns:c.execute(f'ALTER TABLE {table} ADD COLUMN {name} {spec}')
    migrating_requirements = 'requirements' in task_columns or 'criterias' not in task_columns
    if migrating_requirements or 'updated' in task_columns:
        # Store recreates timestamp triggers after the schema changes. The
        # migration itself should not change the task's last activity time.
        c.execute('DROP TRIGGER IF EXISTS business_tasks_timestamps_update')
    if migrating_requirements:
        legacy = 'requirements' if 'requirements' in task_columns else "'{}'"
        for task_id, goal, constraints, criterias, requirements in c.execute(
            f'SELECT id,goal,constraints,criterias,{legacy} FROM business_tasks'
        ).fetchall():
            saved = json.loads(requirements or '{}')
            rules = saved['constraints'] if 'constraints' in saved else json.loads(constraints)
            if isinstance(rules, list):
                rules = {f'constraint_{i}': rule for i, rule in enumerate(rules, 1)}
            criteria = saved.get('criteria', json.loads(criterias))
            c.execute('UPDATE business_tasks SET goal=?,constraints=?,criterias=? WHERE id=?', (
                saved.get('goal', goal), json.dumps(rules,ensure_ascii=False),
                json.dumps(criteria,ensure_ascii=False), task_id,
            ))
    if 'requirements' in task_columns:
        c.execute('ALTER TABLE business_tasks DROP COLUMN requirements')
    if 'updated' in task_columns:
        c.execute('UPDATE business_tasks SET execution_count=updated WHERE execution_count<updated')
        c.execute('DROP INDEX IF EXISTS task_session')
        c.execute('ALTER TABLE business_tasks DROP COLUMN updated')
    c.execute('CREATE INDEX IF NOT EXISTS task_session ON business_tasks(session)')
