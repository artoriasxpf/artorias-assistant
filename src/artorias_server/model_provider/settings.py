import os
from dataclasses import dataclass, field

@dataclass(frozen=True)
class ModelSettings:
    provider: str
    model: str
    api_key: str = field(repr=False)
    base_url: str | None = None
    timeout: float = 60
    max_retries: int = 1
    max_tokens: int = 4096

    @classmethod
    def from_env(cls):
        provider=os.getenv("ARTORIAS_MODEL_PROVIDER","openai").strip().lower()
        prefixes={"openai":"OPENAI","deepseek":"DEEPSEEK","anthropic":"ANTHROPIC"}
        if provider not in prefixes:raise ValueError("ARTORIAS_MODEL_PROVIDER 必须为 openai/deepseek/anthropic")
        prefix=prefixes[provider]
        model=os.getenv("ARTORIAS_MODEL","").strip()
        key=os.getenv(prefix+"_API_KEY","").strip()
        if not model:raise ValueError("请配置 ARTORIAS_MODEL")
        if not key:raise ValueError("请配置 "+prefix+"_API_KEY")
        url=os.getenv(prefix+"_BASE_URL") or {"openai":"https://api.openai.com/v1","deepseek":"https://api.deepseek.com","anthropic":"https://api.anthropic.com"}[provider]
        return cls(provider,model,key,url)
