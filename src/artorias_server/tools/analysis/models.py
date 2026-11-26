"""Contracts for durable, read-only analysis."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

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


class DeferredAnalysis(RuntimeError):
    """A persisted analysis batch is waiting for its endpoint."""


class ExhaustedAnalysis(RuntimeError):
    """The analysis has reached its cumulative scheduling budget."""
