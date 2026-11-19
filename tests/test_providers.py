import pytest
from artorias_server.model_provider.settings import ModelSettings
from artorias_server.model_provider.factory import create_model
from artorias_server.runtime.service import text_content

@pytest.mark.parametrize('provider,module,cls',[('openai','langchain_openai','ChatOpenAI'),('deepseek','langchain_deepseek','ChatDeepSeek'),('anthropic','langchain_anthropic','ChatAnthropic')])
def test_real_constructor(provider,module,cls,monkeypatch):
    # Constructor test: do not inherit execution-host proxy transports.
    import os
    for key in list(os.environ):
        if key.lower().endswith("_proxy"):monkeypatch.delenv(key,raising=False)
    import importlib
    model=create_model(ModelSettings(provider,'test-model','test-key'))
    assert isinstance(model,getattr(importlib.import_module(module),cls))
    assert hasattr(model,'bind_tools')

def test_env_isolation(monkeypatch):
    monkeypatch.setenv('ARTORIAS_MODEL_PROVIDER','deepseek');monkeypatch.setenv('ARTORIAS_MODEL','test')
    monkeypatch.delenv('DEEPSEEK_API_KEY',raising=False);monkeypatch.setenv('OPENAI_API_KEY','wrong-provider')
    with pytest.raises(ValueError,match='DEEPSEEK_API_KEY'):ModelSettings.from_env()
    monkeypatch.setenv('DEEPSEEK_API_KEY','secret');config=ModelSettings.from_env()
    assert config.api_key=='secret' and 'secret' not in repr(config)
    assert config.base_url=='https://api.deepseek.com'

def test_anthropic_blocks():
    assert text_content([{'type':'thinking','thinking':'private'},{'type':'text','text':'hello'},{'type':'text','text':' world'}])=='hello world'
