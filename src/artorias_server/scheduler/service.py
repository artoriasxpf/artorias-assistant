import asyncio

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
        try:
            async for kind,payload in self.runtime.execute(run):
                self.store.emit(run['session'],run['id'],kind,payload)
                if kind=='assistant.final':final=payload['text']
                if kind=='approval.requested':
                    self.store.finish(run,'waiting');return
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
        elif rows[0]['status'] in ('queued','waiting'):
            self.store.finish(rows[0],'cancelled');self.store.emit(rows[0]['session'],rid,'run.cancelled',{})
