from pathlib import Path
from typing import Protocol

class PythonBackend(Protocol):
    location: str
    def profile(self) -> dict: ...
    async def run(self, plan: dict, staging_root: Path) -> dict: ...
