import json
from .requirements import requirements_for

class ContextBuilder:
    def __init__(self, repo, budget=24000):
        self.repo, self.budget = repo, budget
    def build(self, run, step):
        store=self.repo.store
        old=store.query('SELECT body FROM context_snapshots WHERE run=? AND key=?',(run['id'],step['key']))
        if old: return json.loads(old[0]['body'])
        task=store.query('SELECT * FROM business_tasks WHERE id=? AND session=?',(step['task'],run['session']))[0]
        scope=json.loads(step['scope'])
        dependencies=store.query("SELECT key,result FROM task_steps WHERE run=? AND status='delivered'",(run['id'],))
        dependencies=[d for d in dependencies if d['key'] in scope['depends_on']]
        effective=json.loads(step['effective_requirements']) or requirements_for(task)
        task={k:task[k] for k in ('id','status','summary','progress','source_task')} | {'goal':effective['goal'], 'revision':step['applied_revision']}
        task['progress']=json.loads(task['progress'])[-5:]
        task['summary']=task['summary'][-3000:]
        body={'effective_requirements':effective,'selection_policy':'task-results-recent-v4','task':task,'execution_scope':scope,'dependency_results':dependencies,'source_results':[],'history':[]}
        # A character budget, intentionally not advertised as a token budget.
        essential=len(json.dumps(body,ensure_ascii=False))+len(run['input'])
        if essential>self.budget: raise ValueError('Required context exceeds budget; refine the request')
        if task['source_task']:
            source_steps=store.query("""
                SELECT s.run,s.key,s.result FROM task_steps s
                JOIN business_tasks t ON t.id=s.task
                WHERE t.id=? AND t.session=? AND s.status='delivered' AND s.run!=?
                ORDER BY s.updated_at DESC,s.rowid DESC LIMIT 3
            """,(task['source_task'],run['session'],run['id']))
            for source in source_steps:
                result=source['result']
                entry={
                    'result_ref':{'run_id':source['run'],'step_key':source['key']},
                    'total_chars':len(result),
                }
                preview=result[:4000]
                with_preview={**entry,'content':preview,'has_more':len(preview)<len(result)}
                size=len(json.dumps(with_preview,ensure_ascii=False))+100
                if essential+size>self.budget:
                    with_preview={**entry,'content':'','has_more':bool(result)}
                    size=len(json.dumps(with_preview,ensure_ascii=False))+100
                    if essential+size>self.budget:
                        break
                essential+=size
                body['source_results'].append(with_preview)
        recent=store.query("SELECT m.* FROM messages m JOIN runs r ON r.id=m.run WHERE m.role='user' AND m.session=? AND r.status='completed' AND m.run!=? ORDER BY m.rowid DESC LIMIT 6",(run['session'],run['id']))
        related=store.query("SELECT run,key,result FROM task_steps WHERE task=? AND status='delivered' AND run!=? ORDER BY updated_at DESC,rowid DESC LIMIT 5",(step['task'],run['id']))
        related=[{'id':m['run']+':'+m['key'],'run':m['run'],'role':'task_result','content':m['result'][:4000], 'result_ref':{'run_id':m['run'],'step_key':m['key']}} for m in related]
        used=set()
        for m in related+list(reversed(recent)):
            if m['id'] in used: continue
            record={'message_id':m['id'],'run_id':m['run'],'role':m['role'],'content':m['content']}
            if 'result_ref' in m: record['result_ref']=m['result_ref']
            size=len(json.dumps(record,ensure_ascii=False))
            if essential+size+100>self.budget: continue
            essential+=size+100; used.add(m['id']);body['history'].append(record)
        with store.tx() as c:c.execute('INSERT INTO context_snapshots(run,key,body) VALUES(?,?,?)',(run['id'],step['key'],json.dumps(body,ensure_ascii=False)))
        return body
