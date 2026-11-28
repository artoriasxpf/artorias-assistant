import asyncio
import json
from contextlib import aclosing

class Scheduler:
    """Single local worker; replace queue/lease adapter before running multiple processes."""
    def __init__(self,store,runtime):self.store,self.runtime=store,runtime;self.active={}
    async def loop(self):
        self.store.recover()
        while True:
            run=self.store.claim()
            if not run:await asyncio.sleep(.2);continue
            task=asyncio.create_task(self.execute(run));self.active[run['id']]=task
            try:await task
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():raise
            finally:self.active.pop(run['id'],None)
    async def execute(self,run):
        self.store.emit(run['session'],run['id'],'run.started',{})
        final=''
        approval=None
        suspension=None
        try:
            async with aclosing(self.runtime.execute(run)) as events:
                async for kind,payload in events:
                    if kind=='work.idle':
                        with self.store.tx() as db:
                            if db.execute("SELECT 1 FROM work_plans WHERE run=? AND status='running'", (run['id'],)).fetchone():
                                db.execute("UPDATE runs SET status='queued' WHERE id=?",(run['id'],))
                                db.execute("UPDATE jobs SET status='queued' WHERE run=?",(run['id'],))
                                return
                            db.execute("UPDATE runs SET status='work_waiting' WHERE id=?",(run['id'],))
                            db.execute("UPDATE jobs SET status='done' WHERE run=?",(run['id'],))
                            db.execute("INSERT INTO events(session,run,kind,payload) VALUES(?,?,'work.idle','{}')",
                                       (run['session'],run['id']))
                        return
                    if kind=='task.awaiting_review':
                        from artorias_server.tasks.reviews import park_or_requeue
                        park_or_requeue(self.store,run)
                        return
                    if kind in ('analysis.suspended','work.pause','work.requeue'):
                        suspension = (kind,payload)
                        break
                    if kind=='approval.requested':
                        approval=payload
                        break
                    self.store.emit(run['session'],run['id'],kind,payload)
                    if kind=='assistant.final':final=payload['text']
            if suspension is not None:
                kind,suspension = suspension
                from artorias_server.work.repository import park
                if kind == 'work.pause':
                    park(self.store,run,suspension)
                    return
                if kind == 'work.requeue':
                    with self.store.tx() as db:
                        plan = db.execute('SELECT * FROM work_plans WHERE id=? AND run=?',
                                          (suspension['plan_id'],run['id'])).fetchone()
                        from artorias_server.work.repository import suspend_attempt
                        suspend_attempt(db,plan,'重新调度工作单元')
                        self.store.requeue_step(db,run['id'],plan['step_key'])
                    return
                if not self.store.suspend_analysis(run, suspension['dependency_id']):
                    plans = self.store.query("SELECT id FROM work_plans WHERE run=? AND step_key=? AND status='running'",(run['id'],suspension['step_key']))
                    if plans:
                        park(self.store,run,dict(plan_id=plans[0]['id'],reason='自动续跑次数或时限已到'))
                        return
                    raise ValueError('分析续跑次数已达上限，已提交的批次可在后续运行中使用')
                self.store.emit(run['session'], run['id'], 'analysis.suspended', suspension)
                return
            if approval is not None:
                self.store.finish(run,'waiting',approval=approval);return
            self.store.finish(run,'completed',final)
            self.store.emit(run['session'],run['id'],'run.completed',{})
        except asyncio.CancelledError:
            self.store.finish(run,'cancelled');self.store.emit(run['session'],run['id'],'run.cancelled',{});raise
        except Exception as e:
            self.store.finish(run,'failed');self.store.emit(run['session'],run['id'],'run.failed',{'error':str(e)[:1000]})
    def cancel(self,rid):
        rows=self.store.query('SELECT * FROM runs WHERE id=?',(rid,))
        if not rows:raise KeyError(rid)
        if rid in self.active:self.active[rid].cancel()
        elif rows[0]['status'] in ('queued','waiting','awaiting_review','analysis_waiting','work_waiting'):
            if rows[0]['status']=='waiting':
                self._close_pending_approval(rows[0],'cancelled')
            self.store.finish(rows[0],'cancelled');self.store.emit(rows[0]['session'],rid,'run.cancelled',{})
    def _close_pending_approval(self,run,reason):
        request=self.store.query(
            "SELECT seq,payload FROM events WHERE session=? AND run=? AND kind='approval.requested' ORDER BY seq DESC LIMIT 1",
            (run['session'],run['id']),
        )
        if not request:return
        pending=json.loads(request[0]['payload'])
        self.store.emit(run['session'],run['id'],'approval.closed',{
            'task_id':pending.get('task_id'),
            'request_seq':request[0]['seq'],
            'request_ids':[item.get('request_id') for item in pending.get('requests',[]) if isinstance(item,dict)],
            'reason':reason,
        })
