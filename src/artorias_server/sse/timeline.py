"""Small conversation snapshots and cursor-based turn history."""
import json
import sqlite3
from contextlib import closing

def event_row(row):
    return dict(row, payload=json.loads(row['payload']))


def compact_events(rows):
    """Combine adjacent text deltas without altering the original event log."""
    items = []
    for row in rows:
        event = event_row(row)
        if (event['kind'] == 'assistant.delta' and items
                and items[-1]['kind'] == 'assistant.delta'):
            previous = items[-1]
            previous['payload']['text'] += event['payload']['text']
            previous['seq'] = event['seq']
        else:
            items.append(event)
    return items


def latest_event(db, session, run, kind):
    return db.execute(
        'SELECT * FROM events WHERE session=? AND run=? AND kind=? ORDER BY seq DESC LIMIT 1',
        (session, run, kind),
    ).fetchone()


def turn_summary(db, session, row, has_reviews):
    run = row['id']
    rows = []
    for kind in ('assistant.final', 'run.failed', 'run.cancelled', 'run.interrupted'):
        event = latest_event(db, session, run, kind)
        if event:
            rows.append(event)
    if row['status'] == 'waiting':
        pending = latest_event(db, session, run, 'approval.requested')
        if pending:
            rows.append(pending)
    rows.extend(db.execute(
        """SELECT e.* FROM work_plans p JOIN events e ON e.seq=(
          SELECT max(seq) FROM events WHERE session=p.session AND run=p.run
          AND kind='work.plan.waiting' AND json_extract(payload,'$.plan_id')=p.id)
          WHERE p.session=? AND p.run=? AND p.status='waiting'""", (session, run),
    ))
    if has_reviews:
        rows.extend(db.execute(
            """SELECT e.* FROM task_review_requests r JOIN events e ON e.seq=(
              SELECT max(seq) FROM events WHERE session=r.session AND run=r.run
              AND kind='review.requested' AND json_extract(payload,'$.id')=r.id)
              WHERE r.session=? AND r.run=? AND r.state='pending'""", (session, run),
        ))
    resumed = latest_event(db, session, run, 'work.plan.resumed')
    if resumed:
        rows.append(resumed)
    if row['status'] == 'running':
        # Restore the in-flight model bubble. Completed/intermediate bubbles are
        # loaded with the rest of this turn's process.
        boundary = db.execute(
            """SELECT coalesce(max(seq),0) FROM events WHERE session=? AND run=? AND kind IN
            ('tool.call','approval.requested','task.started','task.resumed','task.delivered',
             'run.started','assistant.final')""", (session, run),
        ).fetchone()[0]
        rows.extend(db.execute(
            """SELECT * FROM events WHERE session=? AND run=? AND seq>? AND kind IN
            ('assistant.delta','model.started','model.finished') ORDER BY seq""", (session, run, boundary),
        ))
    events = compact_events(sorted(rows, key=lambda event: event['seq']))
    reply = db.execute(
        "SELECT content FROM messages WHERE session=? AND run=? AND role='assistant' ORDER BY rowid DESC LIMIT 1",
        (session, run),
    ).fetchone()
    return dict(id=run, order=row['position'], number=row['number'], first_seq=row['first_seq'], input=row['input'],
                status=row['status'], reply=reply['content'] if reply else None, events=events)


def require_session(db, session):
    if db.execute('SELECT 1 FROM sessions WHERE id=?', (session,)).fetchone() is None:
        raise KeyError(session)


RUN_COLUMNS = """r.id,r.rowid AS position,r.input,r.status,
    (SELECT count(*) FROM runs WHERE session=r.session AND rowid<=r.rowid) AS number,
    (SELECT min(seq) FROM events WHERE session=r.session AND run=r.id) AS first_seq"""


def page(store, session, before, limit):
    """Read the summaries and SSE high-water mark from the same SQLite snapshot."""
    with closing(sqlite3.connect(store.path)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        require_session(db, session)
        cursor = db.execute('SELECT coalesce(max(seq),0) FROM events WHERE session=?', (session,)).fetchone()[0]
        parameters = [session]
        condition = ''
        if before is not None:
            condition = ' AND r.rowid<?'
            parameters.append(before)
        rows = db.execute(
            f'SELECT {RUN_COLUMNS} FROM runs r WHERE r.session=?{condition} ORDER BY r.rowid DESC LIMIT ?',
            (*parameters, limit + 1),
        ).fetchall()
        selected = rows[:limit]
        next_before = selected[-1]['position'] if len(rows) > limit else None
        first_seq = selected[-1]['first_seq'] if next_before else 0
        until = cursor + 1
        if before is not None:
            boundary = db.execute(
                """SELECT min(e.seq) FROM runs r JOIN events e ON e.session=r.session AND e.run=r.id
                   WHERE r.session=? AND r.rowid=?""", (session, before),
            ).fetchone()[0]
            if boundary is not None:
                until = boundary
        session_events = [event_row(row) for row in db.execute(
            """SELECT * FROM events WHERE session=? AND run IS NULL AND seq>=? AND seq<?
               ORDER BY seq""", (session, first_seq or 0, until),
        )]
        has_reviews = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_review_requests'"
        ).fetchone()
        if before is None:
            visible = {row['id'] for row in selected}
            review_filter = """ OR EXISTS (SELECT 1 FROM task_review_requests review
                WHERE review.session=r.session AND review.run=r.id AND review.state='pending')""" if has_reviews else ''
            active = db.execute(
                f"""SELECT {RUN_COLUMNS} FROM runs r WHERE r.session=? AND (
                 r.status IN ('queued','running','waiting','work_waiting','awaiting_review','analysis_waiting')
                 OR EXISTS (SELECT 1 FROM work_plans p WHERE p.session=r.session AND p.run=r.id AND p.status='waiting')
                 {review_filter})""",
                (session,),
            )
            selected = [*selected, *(row for row in active if row['id'] not in visible)]
        turns = [turn_summary(db, session, row, has_reviews)
                 for row in sorted(selected, key=lambda row: row['position'])]
        return dict(turns=turns, events=session_events, cursor=cursor, next_before=next_before)


def process_page(store, session, run, after, through, limit):
    with closing(sqlite3.connect(store.path)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        if db.execute('SELECT 1 FROM runs WHERE session=? AND id=?', (session, run)).fetchone() is None:
            raise KeyError(run)
        rows = db.execute(
            """SELECT * FROM events WHERE session=? AND run=? AND seq>? AND seq<=?
              AND kind NOT LIKE 'runtime.%' AND kind NOT IN ('analysis.result')
              ORDER BY seq LIMIT ?""", (session, run, after, through, limit + 1),
        ).fetchall()
    return dict(events=compact_events(rows[:limit]), after=rows[limit-1]['seq'] if len(rows)>limit else None)
