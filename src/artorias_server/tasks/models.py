from pydantic import BaseModel, Field
from typing import Literal

class RequirementOperation(BaseModel):
    op: Literal['add', 'replace', 'remove']
    field: Literal['goal', 'constraints', 'criteria']
    id: str | None = Field(default=None, max_length=100)
    value: str | None = Field(default=None, max_length=2000)
    evidence: str = Field(min_length=1, max_length=2000)

class RequirementPatch(BaseModel):
    scope: Literal['persistent', 'execution'] = 'execution'
    expected_revision: int | None = Field(default=None, ge=0)
    operations: list[RequirementOperation] = Field(default_factory=list, max_length=20)

class LocationIntent(BaseModel):
    intent: str = Field(min_length=1, max_length=1000)
    device_id: str | None = None
    workspace_id: str | None = None

class Item(BaseModel):
    key: str
    relation: Literal['new', 'continue', 'refine', 'followup'] = 'new'
    task_id: str | None = None
    goal: str = Field(min_length=1, max_length=2000)
    constraints: list[str] = Field(default_factory=list, max_length=10)
    criteria: list[str] = Field(default_factory=list, max_length=10)
    depends_on: list[str] = Field(default_factory=list)
    task_patch: RequirementPatch = Field(default_factory=RequirementPatch)
    input_locations: list[LocationIntent] = Field(default_factory=list, max_length=10)
    output_target: LocationIntent | None = None

class Plan(BaseModel):
    kind: Literal['chat', 'tasks', 'clarify']
    question: str = ''
    items: list[Item] = Field(default_factory=list, max_length=6)
