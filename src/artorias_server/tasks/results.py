"""Paginated retrieval of delivered results within the current Task only."""
from langchain_core.tools import tool


def result_tool(store, session_id, task_id):
    @tool
    def task_result_read(run_id: str, step_key: str, offset: int = 0, limit: int = 4000) -> dict:
        """Read a delivered result_ref from this Task, in bounded character pages."""
        if offset < 0 or not 1 <= limit <= 8000:
            raise ValueError('Invalid result page')
        rows = store.query(
            "SELECT s.result FROM task_steps s JOIN business_tasks t ON t.id=s.task "
            "WHERE t.session=? AND t.id=? AND s.run=? AND s.key=? AND s.status='delivered'",
            (session_id, task_id, run_id, step_key),
        )
        if not rows:
            raise ValueError('Result not available in this Task')
        result = rows[0]['result']
        return {'content': result[offset:offset+limit], 'offset': offset,
                'total_chars': len(result), 'has_more': offset+limit < len(result)}
    return task_result_read
