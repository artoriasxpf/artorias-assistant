from .models import Plan, FinishStep
from .verification import assess
from .repository import TaskRepository
from .context import ContextBuilder
from .planner import TaskPlanner
import asyncio
import json
from contextlib import aclosing

class TaskRuntime:
    def __init__(self, data, store, engine=None, planner=None, *, text_index=None):
        from artorias_server.runtime.service import DeepAgentRuntime
        self.store=store;self.repo=TaskRepository(store,text_index=text_index);self.context=ContextBuilder(self.repo)
        self.engine=engine or DeepAgentRuntime(data,store);self.planner=planner
        self.task_planner = TaskPlanner(store, planner)
    async def plan(self, run, candidates):
        return await self.task_planner.plan(run, candidates)

    async def execute(self, run):
        stored=self.store.query('SELECT plan FROM task_plans WHERE run=?',(run['id'],))
        if stored:
            plan=Plan.model_validate_json(stored[0]['plan'])
        else:
            candidates=await asyncio.to_thread(self.repo.candidates, run['session'], run['input'])
            plan=self.repo.install(run,await self.plan(run,candidates),candidates)
            yield 'task.plan',plan.model_dump()
            # Surface the requirements patches that actually took effect: the plan
            # only carries intent, the step rows carry the applied revisions.
            for row in self.store.query('SELECT key,task,base_revision,applied_revision,task_patch FROM task_steps WHERE run=? ORDER BY rowid',(run['id'],)):
                patch=json.loads(row['task_patch']) if row.get('task_patch') else {}
                if row['applied_revision'] != row['base_revision']:
                    yield 'task.revised',{'task_id':row['task'],'key':row['key'],'base_revision':row['base_revision'],'applied_revision':row['applied_revision'],'scope':'persistent'}
        if plan.kind=='clarify':
            yield 'assistant.final',{'text':plan.question or '请补充本次要处理的目标。'};return
        if plan.kind=='chat':
            async with aclosing(self.engine.execute(run)) as events:
                async for event in events:yield event
            return
        steps=self.store.query('SELECT * FROM task_steps WHERE run=? ORDER BY rowid',(run['id'],))
        results=[]
        delivered=set()
        pending=False
        for step in steps:
            if step['status']=='needs_replan':
                continue
            if step['status']=='delivered':
                results.append(step['result'])
                if json.loads(step['result_json'])['submitted']['step_outcome']=='completed':
                    delivered.add(step['key'])
                continue
            gates=self.store.query('SELECT state FROM task_step_waits WHERE run=? AND consumer=?',(run['id'],step['key']))
            if any(g['state']=='needs_replan' for g in gates):
                with self.store.tx() as c:
                    c.execute("UPDATE task_steps SET status='needs_replan' WHERE run=? AND key=?",(run['id'],step['key']))
                results.append('验收未通过或已失效，请按反馈修改后继续。')
                yield 'task.needs_replan',{'key':step['key'],'task_id':step['task']}
                continue
            if any(g['state']=='pending' for g in gates) or set(json.loads(step['scope'])['depends_on'])-delivered:
                pending=True
                continue
            task_revision=self.store.query('SELECT revision FROM business_tasks WHERE id=?',(step['task'],))[0]['revision']
            if task_revision != step['applied_revision']:
                raise ValueError('任务要求已修改，当前步骤需要重新规划')
            scoped=dict(run,step_key=step['key'],analysis_plan=json.loads(step['scope']).get('analysis'),task_step_id=step.get('id',step['key']),task_id=step['task'],thread_id=run['id']+':'+step['key'],task_context=self.context.build(run,step))
            if step['status']!='waiting':scoped.pop('decision',None)
            if step['status']=='waiting':
                if not scoped.get('decision'):
                    raise ValueError('任务仍在等待审批，缺少恢复决定')
                yield 'task.resumed',{'task_id':step['task'],'key':step['key'],'reason':'approval'}
            elif step['status']=='interrupted':
                yield 'task.resumed',{'task_id':step['task'],'key':step['key'],'reason':'interrupted'}
            else:
                yield 'task.started',{'task_id':step['task'],'key':step['key']}
            submission=None
            approval=None
            analysis_result = None
            try:
                async with aclosing(self.engine.execute(scoped)) as events:
                    async for kind,payload in events:
                        if kind=='step.submitted':
                            submission=FinishStep.model_validate(payload)
                            continue
                        if kind=='assistant.final':continue
                        if kind == 'analysis.result':
                            analysis_result = payload
                        if kind == 'analysis.suspended':
                            yield kind, payload
                            return
                        if kind=='approval.requested':
                            approval=dict(payload,task_id=step['task'])
                            break
                        yield kind,dict(payload,task_id=step['task'])
                if approval is None:
                    if submission is None:
                        raise ValueError('Missing structured step result')
                    evidence,verdicts=assess(self.store,run,step,submission)
                    status=self.repo.deliver(run,step,submission,evidence,verdicts,analysis_result=analysis_result)
            except asyncio.CancelledError:
                with self.store.tx() as c:c.execute("UPDATE task_steps SET status='interrupted' WHERE run=? AND key=?",(run['id'],step['key']))
                yield 'task.interrupted',{'task_id':step['task'],'key':step['key'],'reason':'cancelled'}
                raise
            except Exception as exc:
                with self.store.tx() as c:c.execute("UPDATE task_steps SET status='failed' WHERE run=? AND key=?",(run['id'],step['key']))
                yield 'task.failed',{'task_id':step['task'],'key':step['key'],'reason':str(exc)[:1000]}
                raise
            if approval is not None:
                # Graph/checkpoint teardown is complete before publishing the pause.
                with self.store.tx() as c:
                    c.execute("UPDATE task_steps SET status='waiting' WHERE run=? AND key=?",(run['id'],step['key']))
                yield 'approval.requested',approval
                return
            results.append(submission.final_text)
            if submission.step_outcome=='completed':
                delivered.add(step['key'])
            yield 'task.delivered',{'task_id':step['task'],'key':step['key'],'text':submission.final_text,
                                    'step_outcome':submission.step_outcome,'status':status}
        if pending:
            yield 'task.awaiting_review',{}
            return
        yield 'assistant.final',{'text':'\n\n'.join(results)}
