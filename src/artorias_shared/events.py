"""Event wire values shared by orchestration and device adapters."""
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Event:
    session: str
    run: str | None
    kind: str
    payload: dict[str, Any]
