"""Approval messages; authorization and persistence remain server responsibilities."""
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class ApprovalRequest:
    kind: str
    path: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_payload(self):
        return {**self.details, 'kind': self.kind, 'path': self.path}


ApprovalDecision = Literal['approve', 'reject']
