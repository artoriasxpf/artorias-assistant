from artorias_server.tasks.requirements import requirements_for

from ..models import Document

COLLECTION = 'tasks'
PROJECTION_VERSION = 'task-goal-rules-summary-12000-v1'

def project(task) -> Document:
    requirements = requirements_for(task)
    body = task['summary']
    if len(body) > 12000:
        body = body[:5999] + '\n\n' + body[-5999:]
    return Document(task['id'], task['goal'], '\n'.join([
        *requirements['constraints'].values(), *requirements['criteria'].values(),
    ]), body)
