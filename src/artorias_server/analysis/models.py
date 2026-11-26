"""Contracts for durable, read-only analysis."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Path = Annotated[str, Field(min_length=1, max_length=512)]
Short = Annotated[str, Field(min_length=1, max_length=500)]


class AnalysisModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class AnalysisPlan(AnalysisModel):
    analysis_type: Literal['document_file', 'document_directory', 'codebase', 'mixed', 'log', 'other']
    intent: Short
    goal: Short
    roots: list[Path] = Field(min_length=1, max_length=8)
    coverage_policy: Literal['full', 'sampled', 'priority_first'] = 'priority_first'


class ReadSpec(AnalysisModel):
    kind: Literal['range', 'inventory', 'search'] = 'range'
    path: Path
    offset: int = Field(default=0, ge=0, le=2**53)
    limit: int = Field(default=2000, ge=4, le=4096)
    unit: Literal['byte', 'unicode_character', 'line'] = 'byte'
    expected_version: str | None = Field(default=None, max_length=128)
    query: str | None = Field(default=None, min_length=1, max_length=128)
    cursor: str | None = Field(default=None, max_length=128)

    @model_validator(mode='after')
    def check_range(self):
        if self.kind == 'search' and not self.query:
            raise ValueError('搜索必须提供 query')
        if self.kind == 'range' and self.offset > (1 if self.unit == 'line' else 0) and not self.expected_version:
            raise ValueError('续读必须提供 expected_version')
        return self


class Finding(AnalysisModel):
    text: Short
    basis: Literal['observation', 'inference', 'hypothesis'] = 'observation'
    refs: list[int] = Field(default_factory=list, max_length=8)


class NoteInput(AnalysisModel):
    batch_id: str = Field(max_length=64)
    summary: Short
    findings: list[Finding] = Field(default_factory=list, max_length=8)
    open_questions: list[Short] = Field(default_factory=list, max_length=6)
    facets: dict[Annotated[str, Field(max_length=48)], Annotated[list[Short], Field(max_length=8)]] = Field(default_factory=dict, max_length=8)


class BatchInput(AnalysisModel):
    previous_note: NoteInput | None = None
    purpose: Short
    reads: list[ReadSpec] = Field(min_length=1, max_length=8)


class DeferredAnalysis(RuntimeError):
    """A persisted analysis batch is waiting for its endpoint."""


class ExhaustedAnalysis(RuntimeError):
    """The analysis has reached its cumulative scheduling budget."""
