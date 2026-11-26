def migrate(database, index):
    with database.transaction(write=True) as conn:
        index.initialize(conn)
        conn.execute('''CREATE TABLE IF NOT EXISTS search_consumers(
            collection TEXT PRIMARY KEY, source_id TEXT NOT NULL, last_seq INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))''')
