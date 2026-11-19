"""Executor selection contracts, independent of LLM tool schemas."""
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolRequest:
    name: str
    arguments: dict[str, Any]
    executor: str | None = None


@dataclass(frozen=True)
class ToolResult:
    status: str
    payload: dict[str, Any]
