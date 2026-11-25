"""Paginated retrieval of current Task and direct source Task deliveries."""
from langchain_core.tools import tool


def result_tool(store, session_id, task_id, consumer_run=None, consumer_key=None):
    @tool
    def task_result_read(run_id: str, step_key: str, offset: int = 0, limit: int = 4000) -> dict:
        """Read a delivered result_ref from this Task or its direct source in this
        session. For a partial preview in source_results, read the exact result
        before saving or reusing it. Pass next_offset as offset to continue.
        """
        if offset < 0 or not 1 <= limit <= 8000:
            raise ValueError('Invalid result page')
        import json
        consumers=store.query('SELECT scope FROM task_steps WHERE task=? AND run=? AND key=?',(task_id,consumer_run,consumer_key))
        scope=json.loads(consumers[0]['scope']) if consumers else {}
        declared=scope.get('depends_on',[])+[w['step_key'] for w in scope.get('step_spec',{}).get('wait_for',[])]
        dependency=consumer_run==run_id and step_key in declared
        rows = store.query(
            "SELECT s.result FROM task_steps s JOIN business_tasks t ON t.id=s.task "
            "JOIN business_tasks current ON current.id=? AND current.session=t.session "
            "WHERE t.session=? AND (t.id=current.id OR t.id=current.source_task OR ?) "
            "AND s.run=? AND s.key=? AND s.status='delivered'",
            (task_id, session_id, dependency, run_id, step_key),
        )
        if not rows:
            raise ValueError('Result not available in this Task or its direct source')
        result = rows[0]['result']
        next_offset = offset+limit if offset+limit < len(result) else None
        return {'content': result[offset:offset+limit], 'offset': offset, 'next_offset': next_offset,
                'total_chars': len(result), 'has_more': next_offset is not None}
    return task_result_read
