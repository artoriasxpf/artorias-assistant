"""Idempotent migrations. UTC timestamps; legacy dates mean migration time."""

NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"


def rename_business_tasks(connection):
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('business_tasks', 'tasks')"
    )}
    if 'business_tasks' not in tables:
        return
    if 'tasks' in tables:
        raise RuntimeError("Both business_tasks and tasks exist. Resolve the table name conflict before migrating.")
    connection.execute('ALTER TABLE business_tasks RENAME TO tasks')
    # ensure_timestamps installs the renamed triggers before the transaction commits.
    connection.execute('DROP TRIGGER IF EXISTS business_tasks_timestamps_insert')
    connection.execute('DROP TRIGGER IF EXISTS business_tasks_timestamps_update')


def ensure_timestamps(connection):
    # FTS5 virtual tables and their shadow tables are owned by SQLite. Adding
    # application columns/triggers to them corrupts the index schema.
    tables = [(row[1],) for row in connection.execute('PRAGMA main.table_list')
              if row[2] == 'table' and not row[1].startswith('sqlite_')]
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
