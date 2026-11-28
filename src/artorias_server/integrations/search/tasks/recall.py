import logging
import re

from artorias_server.tasks.requirements import requirements_for

from ..models import IndexUnavailable, Scope
from .projection import COLLECTION, PROJECTION_VERSION

log = logging.getLogger(__name__)
_TASK_ID = re.compile(r'(?<![a-zA-Z0-9_])[a-fA-F0-9]{32}(?![a-zA-Z0-9_])')


def recall_candidates(store, index, session, text):
    # Fetch remotely before opening the local transaction; hydrate current authority later.
    remote = getattr(index, 'remote', False)
    remote_hits = _keyword_hits(index, None, session, text) if remote else None
    with store.tx() as conn:
        explicit = []
        for task_id in dict.fromkeys(match.group().lower() for match in _TASK_ID.finditer(text)):
            if conn.execute('SELECT 1 FROM tasks WHERE session=? AND id=?',
                            (session, task_id)).fetchone():
                explicit.append(task_id)
                if len(explicit) == 20:
                    break
        hits = remote_hits if remote else _keyword_hits(index, conn, session, text)
        recent = conn.execute('''SELECT id FROM tasks WHERE session=?
            ORDER BY updated_at DESC,rowid DESC LIMIT 8''', (session,)).fetchall()
        sources = {}
        for source, ids in (('explicit', explicit), ('keyword', [hit.id for hit in hits]),
                            ('recent', [row[0] for row in recent])):
            for task_id in ids:
                labels = sources.setdefault(task_id, [])
                if source not in labels:
                    labels.append(source)
        candidates = []
        for task_id, labels in sources.items():
            row = conn.execute('SELECT * FROM tasks WHERE session=? AND id=?',
                               (session, task_id)).fetchone()
            if row is None:
                continue
            candidate = {key: row[key] for key in ('id', 'goal', 'status', 'revision', 'source_task', 'updated_at')}
            candidate['summary'] = row['summary'][-2000:]
            candidate['effective_requirements'] = requirements_for(row)
            candidate['recall_sources'] = labels
            candidates.append(candidate)
            if len(candidates) == 20:
                break
        return candidates


def _keyword_hits(index, conn, session, text):
    try:
        return index.search(conn, Scope(COLLECTION, session), text[:30000],
                                 projection_version=PROJECTION_VERSION, limit=10)
    except IndexUnavailable as exc:
        log.warning('task.recall degraded: %s', exc)
        return []
