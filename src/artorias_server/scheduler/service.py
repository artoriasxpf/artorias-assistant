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
                    if kind == 'analysis.suspended':
                        suspension = payload
                        break
                    if kind=='approval.requested':
                        approval=payload
                        break
                    self.store.emit(run['session'],run['id'],kind,payload)
                    if kind=='assistant.final':final=payload['text']
            if suspension is not None:
                if not self.store.suspend_analysis(run, suspension['dependency_id']):
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
        elif rows[0]['status'] in ('queued','waiting','analysis_waiting'):
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
