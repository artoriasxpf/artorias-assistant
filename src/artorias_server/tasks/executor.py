from .models import Plan
from .repository import TaskRepository
from .context import ContextBuilder
from .planner import TaskPlanner

class TaskRuntime:
    def __init__(self, data, store, engine=None, planner=None):
        from artorias_server.runtime.service import DeepAgentRuntime
        self.store=store;self.repo=TaskRepository(store);self.context=ContextBuilder(self.repo)
        self.engine=engine or DeepAgentRuntime(data,store);self.planner=planner
        self.task_planner = TaskPlanner(store, planner)
    async def plan(self, run, candidates):
        return await self.task_planner.plan(run, candidates)

    async def execute(self, run):
        stored=self.store.query('SELECT plan FROM task_plans WHERE run=?',(run['id'],))
        if stored:plan=Plan.model_validate_json(stored[0]['plan'])
        else:
            candidates=self.repo.candidates(run['session'],run['input'])
            plan=self.repo.install(run,await self.plan(run,candidates),candidates)
            yield 'task.plan',plan.model_dump()
        if plan.kind=='clarify':
            yield 'assistant.final',{'text':plan.question or '请补充本次要处理的目标。'};return
        if plan.kind=='chat':
            async for event in self.engine.execute(run):yield event
            return
        steps=self.store.query('SELECT * FROM task_steps WHERE run=? ORDER BY rowid',(run['id'],))
        results=[]
        for step in steps:
            if step['status']=='delivered':results.append(step['result']);continue
            scoped=dict(run,task_step_id=step.get('id',step['key']),task_id=step['task'],thread_id=run['id']+':'+step['key'],task_context=self.context.build(run,step))
            if step['status']!='waiting':scoped.pop('decision',None)
            yield 'task.started',{'task_id':step['task'],'key':step['key']}
            final=''
            try:
                async for kind,payload in self.engine.execute(scoped):
                    if kind=='assistant.final': final=payload['text'];continue
                    if kind=='approval.requested':
                        with self.store.tx() as c:c.execute("UPDATE task_steps SET status='waiting' WHERE run=? AND key=?",(run['id'],step['key']))
                        yield kind,dict(payload,task_id=step['task']);return
                    yield kind,dict(payload,task_id=step['task'])
            except BaseException:
                with self.store.tx() as c:c.execute("UPDATE task_steps SET status='interrupted' WHERE run=? AND key=?",(run['id'],step['key']))
                raise
            self.repo.deliver(run, step, final)
            results.append(final)
            yield 'task.delivered',{'task_id':step['task'],'text':final,'status':'needs_review'}
        yield 'assistant.final',{'text':'\n\n'.join(results)}
