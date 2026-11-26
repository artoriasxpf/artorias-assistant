"""Application use cases independent of FastAPI and model vendors."""
class AssistantService:
    def __init__(self,repository,scheduler):
        self.repository,self.scheduler=repository,scheduler
    def create_session(self):return {"id":self.repository.session()}
    def submit_message(self,session,text,request_key):
        return self.repository.submit(session,text,request_key)
    def cancel_run(self,run):
        if self.scheduler is None:
            from artorias_shared.workspace import ConnectorUnavailable
            raise ConnectorUnavailable('取消运行需要配置调度器控制适配器')
        self.scheduler.cancel(run)
        return {"requested":True}
    def decide(self,run,decision,request_seq=None):
        self.repository.decide(run,decision,request_seq)
        return {"accepted":True}
