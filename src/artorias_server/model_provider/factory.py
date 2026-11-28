from urllib.parse import urlsplit

from .settings import ModelSettings

def create_model(settings: ModelSettings | None = None):
    settings=settings or ModelSettings.from_env()
    kwargs=dict(model=settings.model,api_key=settings.api_key,base_url=settings.base_url,timeout=settings.timeout,max_retries=settings.max_retries)
    if settings.base_url is None:kwargs.pop("base_url")
    if settings.provider=="openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(**kwargs)
    if settings.provider=="deepseek":
        from .deepseek import DeepSeekChatModel
        return DeepSeekChatModel(**kwargs)
    if settings.provider=="anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(**kwargs,max_tokens=settings.max_tokens)
    raise ValueError("Unsupported model provider: "+settings.provider)


def prefer_json_object(model):
    """Request native JSON output on endpoints with known support."""
    from .deepseek import DeepSeekChatModel

    if isinstance(model, DeepSeekChatModel):
        hostname = urlsplit(str(model.openai_api_base)).hostname
        if hostname == 'api.deepseek.com':
            return model.bind(response_format={'type': 'json_object'})
    return model
